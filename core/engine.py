"""
主引擎：协调 MOCA、策略、风控、API 各模块
"""

import os
import sys
import time
import json
import threading
import signal
from typing import Dict, Optional

import yaml

from utils.logger import init_logger, get_logger
from utils.notifier import Notifier
from utils.ai_trader import AITraderClient
from utils.ws_client import WSClientManager
from utils.database import Database
from utils.recorder import PnLRecorder
from core.moca_engine import MOCAEngine, MarketState
from risk.risk_manager import RiskManager
from risk.risk_gate import RiskGate
from risk.account_budget import AccountBudgetAllocator
from risk.account_state import AccountRiskState
from strategies.spot_grid import SpotGridStrategy
from utils.helpers import safe_float
from utils.execution_guard import ExecutionGuard
from strategies.futures_hedge import FuturesHedgeStrategy
from execution.event_ledger import EventLedger
from execution.gateway import ExecutionGateway
from execution.reconciler import ExchangeReconciler
from control.command_bus import CommandBus
from core.config_validation import validate_config_bundle


class MainEngine:
    """量化交易主引擎"""

    def __init__(self, config_path: str = "config/config.yaml",
                 api_keys: Optional[Dict[str, str]] = None,
                 extra_configs: Optional[list] = None):
        self._running = False
        self._paused = False
        self._config_path = config_path
        self._api_keys = api_keys
        self._tf_cache = {}
        self._db_run_id = None
        self._hb_tick = 0
        self._config_mtime = 0.0
        self._extra_configs = extra_configs or []
        self._prev_prices = {}
        self._moca_engines = {}
        self._execution_guard = None
        self.execution_gateway = None
        self.risk_gate = None
        self.event_ledger = None
        self.reconciler = None
        self.command_bus = None
        self.account_state = None

        # 加载配置
        self.config = self._load_config(config_path)
        extra_payload = []
        for extra_path in self._extra_configs:
            try:
                with open(extra_path, "r", encoding="utf-8") as handle:
                    extra_payload.append((extra_path, yaml.safe_load(handle) or {}))
            except Exception as exc:
                extra_payload.append((extra_path, {"_load_error": str(exc)}))
        self.config_validation = validate_config_bundle(
            self.config, config_path, extra_payload
        )
        if not self.config_validation["ok"]:
            details = "; ".join(self.config_validation["errors"])
            raise ValueError(f"configuration validation failed: {details}")
        risk_cfg = self.config.get("risk", {}) if isinstance(self.config.get("risk", {}), dict) else {}
        account_budget_cfg = risk_cfg.get("account_budget", {}) if isinstance(risk_cfg.get("account_budget", {}), dict) else {}
        self.account_budget = AccountBudgetAllocator(
            target_margin_ratio=account_budget_cfg.get("target_margin_ratio", 0.45),
            reserve_equity_ratio=account_budget_cfg.get("reserve_equity_ratio", 0.10),
            utilization=account_budget_cfg.get("utilization", 0.80),
            reservation_ttl_sec=account_budget_cfg.get("reservation_ttl_sec", 20.0),
        )
        self._data_dir = str(self.config.get("user_data_dir") or "data")
        os.makedirs(self._data_dir, exist_ok=True)
        self.log = None

    def _load_config(self, path: str) -> dict:
        """加载YAML配置文件"""
        if not os.path.exists(path):
            print(f"[错误] 配置文件不存在: {path}")
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _clone_config(self, config: dict) -> dict:
        return json.loads(json.dumps(config or {}))

    def _symbol_slug(self, symbol: str) -> str:
        raw = (symbol or "").split("-")[0].strip().lower()
        slug = "".join(ch if ch.isalnum() else "_" for ch in raw)
        while "__" in slug:
            slug = slug.replace("__", "_")
        return slug.strip("_") or "strategy"

    def _strategy_runtime_name(self, symbol: str, is_primary: bool) -> str:
        if is_primary:
            return "futures"
        return f"futures_{self._symbol_slug(symbol)}"

    def _build_strategy_config(self, config: dict, is_primary: bool) -> dict:
        cfg = self._clone_config(config)
        if getattr(self, "_data_dir", ""):
            cfg["user_data_dir"] = str(self._data_dir)
        futures_cfg = cfg.get("futures", {}) or {}
        symbol = futures_cfg.get("symbol", "")
        profile = cfg.get("profile", {}) if isinstance(cfg.get("profile"), dict) else {}
        profile.setdefault("slug", "base" if is_primary else self._symbol_slug(symbol))
        profile.setdefault("name", symbol or profile["slug"])
        profile["runtime_name"] = self._strategy_runtime_name(symbol, is_primary)
        profile["is_primary"] = is_primary
        cfg["profile"] = profile
        return cfg

    def _strategy_symbol(self, strategy) -> str:
        return str(getattr(strategy, "_symbol", "") or "")

    def _configured_futures_symbols(self):
        symbols = set()
        if self.config.get("global", {}).get("enable_futures", True):
            symbol = self.config.get("futures", {}).get("symbol", "")
            if symbol:
                symbols.add(symbol)
        for config_path in self._extra_configs:
            try:
                with open(config_path, encoding="utf-8") as handle:
                    config = yaml.safe_load(handle) or {}
                symbol = config.get("futures", {}).get("symbol", "")
                if symbol:
                    symbols.add(symbol)
            except Exception as exc:
                if getattr(self, "log", None):
                    self.log.warning(f"[config] symbol discovery failed {config_path}: {exc}")
        return symbols

    def _get_symbol_price(self, symbol: str) -> float:
        if not symbol:
            return 0.0
        try:
            if hasattr(self, "ws_manager") and self.ws_manager:
                price = self.ws_manager.get_price(symbol)
                if price:
                    return price
        except:
            pass
        try:
            if self.client:
                ticker = self.client.get_ticker(symbol)
                if ticker:
                    return ticker.get("last", 0.0)
        except:
            pass
        return 0.0

    def _symbol_risk_manager(self, name: str, strategy):
        if not hasattr(self, "_symbol_risk_managers"):
            self._symbol_risk_managers = {}
        mgr = self._symbol_risk_managers.get(name)
        if mgr:
            return mgr
        cfg = getattr(strategy, "_engine_config", self.config) or self.config
        mgr = RiskManager(
            cfg,
            client=self.client,
            execution_gateway=getattr(self, "execution_gateway", None),
        )
        mgr.black_swan._on = lambda: None
        mgr.liquidation._on_lev = lambda *_: None
        mgr.liquidation._on_pos = lambda *_: None
        mgr.liquidation._on_emer = lambda: None
        self._symbol_risk_managers[name] = mgr
        return mgr

    def _pause_strategy_openings(self, strategy, reason: str, seconds: float):
        pause = getattr(strategy, "pause_opening", None)
        if callable(pause):
            pause(reason, seconds, cancel_existing=True)
            return
        if getattr(self, "log", None):
            self.log.warning("[risk] strategy lacks opening-only pause support; leave exits running")

    @staticmethod
    def _publish_liquidation_status(strategy, status: dict, pause_reason: str = ""):
        """Publish one normalized risk snapshot for strategy status and Web control."""
        payload = dict(status or {})
        pause = getattr(strategy, "opening_pause_status", None)
        pause_state = pause() if callable(pause) else {}
        payload["opening_pause_active"] = bool(pause_state.get("active", False))
        payload["opening_pause_reason"] = str(
            pause_state.get("reason") or pause_reason or ""
        )
        payload["opening_pause_remaining_sec"] = round(
            safe_float(pause_state.get("remaining_sec", 0.0)), 1
        )
        setter = getattr(strategy, "set_liquidation_status", None)
        if callable(setter):
            setter(payload)
        else:
            strategy._liquidation_status = payload

    def _reduce_liquidation_risk(self, strategy, risk_data: dict, sides, ratio: float):
        gateway = getattr(self, "execution_gateway", None)
        symbol = self._strategy_symbol(strategy)
        if gateway is None or not symbol:
            return []
        results = []
        step = max(safe_float(getattr(strategy, "_qty_step", 0.0)), 0.0)
        minimum = max(safe_float(getattr(strategy, "_min_qty", 0.0)), step)
        ratio = min(0.5, max(0.05, safe_float(ratio)))
        for pos_side in sides or []:
            qty = abs(safe_float(risk_data.get(f"{pos_side}_sz", 0.0))) * ratio
            if step > 0:
                qty = int(qty / step) * step
            if qty < minimum:
                continue
            side = "sell" if pos_side == "long" else "buy"
            result = gateway.place_order(
                symbol,
                side,
                str(qty),
                ord_type="market",
                pos_side=pos_side,
                reduce_only=True,
            )
            results.append(result)
        return results

    def _check_symbol_risk(self, name: str, strategy, price: float, prev_price: float):
        symbol = self._strategy_symbol(strategy)
        if not symbol or price <= 0:
            self.log.warning(f"[risk] skip symbol={symbol or name} price={price}")
            return
        try:
            risk_data = {"margin": 0, "notional": 0, "mark_px": price}
            risk_data_error = False
            if self.client:
                try:
                    fetched_risk = self.client.get_position_risk(symbol)
                    risk_data = fetched_risk or risk_data
                    risk_data["mark_px"] = safe_float(risk_data.get("mark_px")) or price
                    risk_data_error = not bool(risk_data.get("positions_available", False))
                except Exception as exc:
                    risk_data_error = True
                    self.log.warning(f"[risk] {symbol} liquidation data unavailable: {exc}")
            cfg = getattr(strategy, "_engine_config", self.config) or self.config
            lev = getattr(strategy, "_cur_lev", cfg.get("futures", {}).get("leverage", 3))
            risk_mgr = self._symbol_risk_manager(name, strategy)
            risk_mgr.black_swan.check(price, prev_price)
            if risk_mgr.black_swan.is_frozen:
                risk_mgr.black_swan.check_thaw()
            positions_present = (
                abs(safe_float(risk_data.get("long_sz", 0))) > 0
                or abs(safe_float(risk_data.get("short_sz", 0))) > 0
            )
            liq_status = {
                "available": False,
                "positions_present": positions_present,
                "zone": "unavailable" if positions_present else "no_positions",
                "liq_px_unavailable": positions_present,
                "liquidation_distance_safe": not positions_present,
                "liquidation_warning": False,
                "liquidation_emergency": False,
                "mark_px": safe_float(risk_data.get("mark_px")) or price,
                "pause_opening": False,
                "emergency_reduce": False,
                "reason": "liquidation_data_unavailable" if risk_data_error else "no_positions",
            }
            if self.client and not risk_data_error:
                if risk_mgr.black_swan.is_frozen:
                    liq_status = risk_mgr.liquidation.last_status
                else:
                    liq_status = risk_mgr.liquidation.evaluate(risk_data)
                liq_status["mark_px"] = safe_float(risk_data.get("mark_px")) or price
                liq_status["long_liq"] = safe_float(risk_data.get("long_liq"))
                liq_status["short_liq"] = safe_float(risk_data.get("short_liq"))
            pause_reason = ""
            pause_seconds = 0.0
            if risk_mgr.black_swan.is_frozen:
                pause_reason = "abnormal_market"
                pause_seconds = max(3.0, risk_mgr.black_swan.remaining)
            elif risk_data_error:
                pause_reason = "liquidation_data_unavailable"
                pause_seconds = risk_mgr.liquidation.pause_seconds
            elif liq_status.get("pause_opening"):
                pause_reason = str(liq_status.get("reason") or "liquidation_proximity")
                pause_seconds = risk_mgr.liquidation.pause_seconds
            if pause_reason:
                self._pause_strategy_openings(strategy, pause_reason, pause_seconds)
                self.log.warning(f"[risk] {symbol} opening paused reason={pause_reason}")
                if getattr(self, "notifier", None):
                    self.notifier.alert(
                        "开仓临时暂停",
                        f"{symbol} 暂停新开仓，止盈和减仓继续，原因: {pause_reason}",
                        key=f"opening_pause:{symbol}:{pause_reason}",
                    )
            self._publish_liquidation_status(strategy, liq_status, pause_reason)
            if liq_status.get("emergency_reduce"):
                self._reduce_liquidation_risk(
                    strategy,
                    risk_data,
                    liq_status.get("affected_sides") or [],
                    risk_mgr.liquidation.reduce_ratio,
                )
                self.log.warning(
                    f"[risk] {symbol} liquidation emergency reduce "
                    f"distance={safe_float(liq_status.get('min_distance_rate')):.2%}"
                )
        except Exception as e:
            self.log.warning(f"[risk] {symbol or name} check failed: {e}")

    def start(self):
        """启动引擎"""
        self._running = True

        # 1. 初始化日志
        init_logger(self.config)
        self.log = get_logger("engine")
        self.log.info("=" * 50)
        self.log.info("OKX 量化交易系统 启动中...")
        self.log.info("=" * 50)
        self.log.info(
            "[config] validation ok profiles=%s warnings=%s",
            len(self.config_validation.get("profiles", [])),
            len(self.config_validation.get("warnings", [])),
        )
        for warning in self.config_validation.get("warnings", []):
            self.log.warning(f"[config] drift warning: {warning}")

        # 2. 初始化 MOCA
        self.log.info("[MOCA] 初始化自适应引擎...")
        self.moca = MOCAEngine(self.config)

        # 3. 初始化盈亏记录
        self.log.info("[Recorder] 初始化记录系统...")
        self.recorder = PnLRecorder(self.config)

        # 4. 初始化 API 客户端（如果提供了密钥）
        self.client = None
        try:
            self.event_ledger = EventLedger(os.path.join(self._data_dir, "trading.db"))
            self.log.info("[Ledger] order event ledger ready")
        except Exception as e:
            self.event_ledger = None
            self.log.warning(f"[Ledger] shadow ledger unavailable: {e}")
            if self._api_keys and self.config.get("global", {}).get("enable_futures", True):
                raise RuntimeError("live futures requires the event ledger") from e
        try:
            self.command_bus = CommandBus(os.path.join(self._data_dir, "control.db"))
            self.log.info("[Control] command bus ready")
        except Exception as e:
            self.command_bus = None
            self.log.warning(f"[Control] command bus unavailable: {e}")
            if self._api_keys and self.config.get("global", {}).get("enable_futures", True):
                raise RuntimeError("live futures requires the command bus") from e
        if self._api_keys:
            try:
                from api.okx_client import OKXClient
                flag = self.config.get("exchange", {}).get("okx", {}).get("flag", "1")
                self.client = OKXClient(self._api_keys, flag=flag, event_ledger=self.event_ledger)
                self.execution_gateway = ExecutionGateway(self.client)
                self.log.info(f"[API] 客户端初始化完成 (flag={flag})")
                self._execution_guard = ExecutionGuard(self.client)
            except Exception as e:
                self.log.warning(f"[API] 客户端初始化失败: {e}")
                self.log.warning("[API] 将在模拟模式下运行")

        self.account_state = (
            AccountRiskState(self.client, self.log) if self.client is not None else None
        )


        # AI-Trader 客户端（策略信号同步）
        self.ai_trader = None
        try:
            self.ai_trader = AITraderClient()
            if self.ai_trader.ready:
                self.log.info('[AI-Trader] 客户端已就绪')
                val = self.ai_trader.get_portfolio_value()
                self.log.info(f'[AI-Trader] 组合价值: {val:.2f} USD')
            else:
                self.log.info('[AI-Trader] 未配置，跳过')
        except Exception as e:
            self.log.warning(f'[AI-Trader] 初始化失败: {e}')
            self.ai_trader = None
        # 5. 初始化风控
        self.log.info("[Risk] 初始化风控...")
        self.risk = RiskManager(
            self.config,
            client=self.client,
            execution_gateway=self.execution_gateway,
        )

        # 6. 初始化通知
        self.notifier = Notifier(self.config)
        self.notifier.attach_error_logger(get_logger())

        # 7. 初始化数据库
        self.db = Database(os.path.join(self._data_dir, "trading.db"))
        self._last_equity_record = 0.0
        self._snapshot_tick = 0
        if self._api_keys:
            self.notifier.send(f"🤖 系统启动 | 模式: 实盘")
        # 记录初始净值
        if self.client:
            try:
                eq = self.client.get_total_equity()
                self.risk.total_loss.set_initial_equity(eq)
                self.log.info(f"[风控] 初始权益: {eq:.2f} USD")
            except:
                pass

        # 写PID文件（供状态检查）
        try:
            with open(os.path.join(self._data_dir, "run.pid"), "w") as pf:
                pf.write(str(os.getpid()))
        except:
            pass
        # WS行情 - subscribe all symbols
        ws_symbols = self._configured_futures_symbols()
        if self.client and self.event_ledger and ws_symbols:
            interval = safe_float(
                self.config.get("global", {}).get("reconciliation_interval_sec", 30)
            )
            self.reconciler = ExchangeReconciler(
                self.client,
                self.event_ledger,
                ws_symbols,
                log=self.log,
                interval=interval,
            )
            self.reconciler.reconcile_all()
            max_age = safe_float(
                self.config.get("global", {}).get("reconciler_max_age_sec", 90)
            )
            self.risk_gate = RiskGate(
                self.event_ledger,
                reconciler=self.reconciler,
                log=self.log,
                max_reconciler_age=max_age,
            )
            self.execution_gateway.set_risk_gate(self.risk_gate)
        self._require_live_futures_guard(ws_symbols)
        self.ws_manager = WSClientManager()
        for sym in sorted(ws_symbols):
            self.ws_manager.subscribe(sym)
            self.log.info(f"[WS] Subscribe: {sym}")
        try:
            sym = self.config.get("futures", {}).get("symbol", "")
            self._db_run_id = self.db.start_run("futures_hedge", sym, os.getpid())
            self.log.info(f"[DB] 运行记录创建 id={self._db_run_id}")
        except Exception as e:
            self.log.warning(f"[DB] 创建运行记录失败: {e}")

        # 8. 初始化策略
        self._init_strategies()
        futures = [
            strategy
            for name, strategy in self.strategies.items()
            if name.startswith("futures")
        ]
        if futures and all(bool(getattr(strategy, "_trading_halted", False)) for strategy in futures):
            self.risk_gate.set_state(
                "HALTED", "all_futures_strategies_halted", actor="engine_startup"
            )

        # 9. 启动主循环
        self.log.info("[Engine] 主循环启动")
        self._main_loop()

    def _require_live_futures_guard(self, ws_symbols):
        """Refuse live futures startup when the final write guard is absent."""
        live_futures = bool(
            self.client is not None
            and self.config.get("global", {}).get("enable_futures", True)
        )
        if not live_futures:
            return
        if not self.event_ledger:
            raise RuntimeError("live futures requires the event ledger")
        if self.command_bus is None:
            raise RuntimeError("live futures requires the command bus")
        if not ws_symbols:
            raise RuntimeError("live futures requires configured futures symbols")
        if self.risk_gate is None or self.reconciler is None:
            raise RuntimeError("live futures requires RiskGate and reconciler")
        status = self.reconciler.get_status()
        if not status.get("ready"):
            raise RuntimeError("live futures requires a ready exchange reconciliation")

    def _init_strategies(self):
        self.strategies = {}
        if self.config.get("global",{}).get("enable_spot",True):
            try:
                spot = SpotGridStrategy(self.config,self.client)
                spot._notifier=self.notifier;spot._db=self.db;spot.restore_state();spot.start()
                self.strategies["spot"]=spot
                self.log.info("[strategy] spot loaded")
            except Exception as e: self.log.error(f"[strategy] spot fail: {e}")
        if self.config.get("global",{}).get("enable_futures",True):
            sym=self.config.get("futures",{}).get("symbol","")
            if sym:
                try:
                    base_cfg = self._build_strategy_config(self.config, True)
                    fut=FuturesHedgeStrategy(base_cfg,self.client)
                    fut._execution_gateway = self.execution_gateway
                    fut._account_budget_allocator = self.account_budget
                    if self.account_state is not None:
                        fut._account_state = self.account_state
                    fut._symbol=sym;fut._notifier=self.notifier;fut._db=self.db;fut.start()
                    fut._engine_config = base_cfg
                    self.strategies["futures"]=fut
                    self._moca_engines["futures"] = MOCAEngine(base_cfg)
                    self.log.info(f"[strategy] hedge {sym} loaded")
                except Exception as e: self.log.error(f"[strategy] hedge fail: {e}")
        for _cfg in self._extra_configs:
            try:
                with open(_cfg) as _f: _sc=yaml.safe_load(_f)
                _sym=_sc.get("futures",{}).get("symbol","")
                if not _sym: continue
                _nm=_sym.split("-")[0].lower()
                runtime_name = self._strategy_runtime_name(_sym, False)
                _cfg_data = self._build_strategy_config(_sc, False)
                _fut=FuturesHedgeStrategy(_cfg_data,self.client)
                _fut._execution_gateway = self.execution_gateway
                _fut._account_budget_allocator = self.account_budget
                if self.account_state is not None:
                    _fut._account_state = self.account_state
                _fut._symbol=_sym;_fut._notifier=self.notifier;_fut._db=self.db;_fut.start()
                _fut._engine_config = _cfg_data
                self.strategies[runtime_name]=_fut
                self._moca_engines[runtime_name] = MOCAEngine(_cfg_data)
                self.log.info(f"[strategy] multi {_sym} loaded")
            except Exception as e: self.log.error(f"[Multi] {_cfg}: {e}")
    def start_strategy(self,name):
        if name in self.strategies:
            try: self.strategies[name].start();self.log.info(f"[strategy] {name} started")
            except Exception as e: self.log.error(f"[strategy] {name} fail: {e}")
        else: self.log.warning(f"[strategy] {name} unknown")
    def stop_strategy(self,name):
        if name in self.strategies:
            try: self.strategies[name].stop();self.log.info(f"[strategy] {name} stopped")
            except Exception as e: self.log.error(f"[strategy] {name} fail: {e}")
        else: self.log.warning(f"[strategy] {name} unknown")

    def _consume_control_commands(self):
        if not self.command_bus:
            return
        for command in self.command_bus.claim_pending():
            command_id = command["command_id"]
            command_type = command["command_type"]
            target = command["target"]
            payload = command["payload"]
            try:
                if command_type == "strategy_start":
                    if target not in self.strategies:
                        raise ValueError(f"unknown strategy: {target}")
                    self.start_strategy(target)
                elif command_type == "strategy_stop":
                    if target not in self.strategies:
                        raise ValueError(f"unknown strategy: {target}")
                    self.stop_strategy(target)
                elif command_type in {"strategy_resume", "risk_resume"}:
                    strategy = self.strategies.get(target)
                    if strategy is None:
                        raise ValueError(f"unknown strategy: {target}")
                    resume = getattr(strategy, "resume", None)
                    if not callable(resume):
                        raise RuntimeError(f"strategy {target} has no manual resume API")
                    result = resume(str(command.get("actor") or payload.get("operator") or "control"))
                    if not result.get("ok", False):
                        raise RuntimeError(str(result.get("reason") or "resume rejected"))
                elif command_type == "risk_state_set":
                    if not self.risk_gate:
                        raise RuntimeError("risk gate unavailable")
                    self.risk_gate.set_state(
                        str(payload.get("state") or ""),
                        str(payload.get("reason") or "control command"),
                        actor=str(command.get("actor") or "control"),
                    )
                elif command_type == "engine_pause":
                    self._paused = True
                elif command_type == "engine_resume":
                    self._paused = False
                else:
                    raise ValueError(f"unsupported command: {command_type}")
                self.command_bus.complete(
                    command_id,
                    {"command_type": command_type, "target": target},
                )
            except Exception as exc:
                self.command_bus.fail(command_id, str(exc))
                self.log.warning(f"[Control] command failed id={command_id}: {exc}")
    def _submit_strategy_action(self, strategy, action: dict):
        client = getattr(strategy, "_client", None) or getattr(self, "client", None)
        gateway = (
            getattr(strategy, "_execution_gateway", None)
            or getattr(self, "execution_gateway", None)
        )
        if gateway is None and client:
            gateway = ExecutionGateway(client)
        if gateway is None or not isinstance(action, dict):
            return None
        side = str(action.get("action") or "").lower()
        qty = safe_float(action.get("quantity", 0))
        if side not in {"buy", "sell"} or qty <= 0:
            return None
        order_type = str(action.get("order_type") or action.get("ord_type") or "market").lower()
        pos_side = str(action.get("pos_side") or "").lower()
        reduce_only = bool(action.get("reduce_only", False))
        price = safe_float(action.get("price", 0))
        symbol = self._strategy_symbol(strategy)
        strategy_submit = getattr(strategy, "_submit_order", None)
        if not reduce_only and not callable(strategy_submit):
            opening_guard = getattr(strategy, "_opening_order_block_reason", None)
            guard_reason = opening_guard(pos_side, qty, price) if callable(opening_guard) else ""
            if guard_reason:
                if getattr(self, "log", None):
                    self.log.warning(
                        f"[opening_guard] skip {symbol} {pos_side} qty={qty} price={price} reason={guard_reason}"
                    )
                return {"sCode": "opening_guard", "sMsg": guard_reason, "guard_blocked": True}
        try:
            if order_type == "limit":
                guard = getattr(self, "_execution_guard", None)
                if guard is not None:
                    allowed, guard_reason = guard.check_order(symbol, side, order_type, price, reduce_only)
                    if not allowed:
                        result = {"sCode": "maker_guard", "sMsg": guard_reason, "guard_blocked": True}
                        if getattr(self, "log", None):
                            self.log.info(f"[执行门禁] skip {symbol} {side} limit px={price} reason={guard_reason}")
                        return result
                if callable(strategy_submit):
                    result = strategy_submit(
                        symbol,
                        side,
                        qty,
                        ord_type="limit",
                        px=price if price > 0 else None,
                        pos_side=pos_side,
                        reduce_only=reduce_only,
                    )
                else:
                    result = gateway.place_order(
                        symbol,
                        side,
                        str(qty),
                        ord_type="limit",
                        pos_side=pos_side,
                        reduce_only=reduce_only,
                        px=str(price) if price > 0 else None,
                    )
            else:
                if callable(strategy_submit):
                    result = strategy_submit(
                        symbol,
                        side,
                        qty,
                        ord_type="market",
                        pos_side=pos_side,
                        reduce_only=reduce_only,
                    )
                else:
                    result = gateway.place_order(
                        symbol,
                        side,
                        str(qty),
                        ord_type="market",
                        pos_side=pos_side,
                        reduce_only=reduce_only,
                    )
        except Exception as exc:
            result = {"sCode": "-1", "sMsg": str(exc), "error": str(exc)}
            if getattr(self, "log", None):
                self.log.warning(f"[strategy] {symbol} action failed: {exc}")
        result = self._normalize_order_result(result)
        on_result = getattr(strategy, "on_order_result", None)
        if callable(on_result):
            try:
                on_result(result)
            except Exception as exc:
                if getattr(self, "log", None):
                    self.log.warning(f"[strategy] order result hook failed: {exc}")
        return result

    def _normalize_order_result(self, result: dict):
        if not isinstance(result, dict):
            return result
        normalized = dict(result)
        payload = result.get("data")
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            first = payload[0]
            for key in ("ordId", "orderId", "clOrdId", "sCode", "sMsg", "tag"):
                if key in first and key not in normalized:
                    normalized[key] = first[key]
        return normalized

    def _handle_strategy_result(self, strategy, result):
        if not isinstance(result, dict):
            return
        actions = result.get("actions")
        if isinstance(actions, list) and actions:
            for action in actions:
                self._submit_strategy_action(strategy, action)
            return
        if result.get("type") == "layer_tp" and result.get("action"):
            self._submit_strategy_action(strategy, result)

    def _get_current_price(self, symbol: str = "") -> float:
        sym = symbol or self.config.get("futures", {}).get("symbol", "BTC-USDT-SWAP")
        return self._get_symbol_price(sym)

    def _record_equity(self):
        """每 60 秒把账户总权益写入 trading.db 的 equity 表（净值曲线落库）。"""
        now = time.time()
        if now - getattr(self, "_last_equity_record", 0.0) < 60.0:
            return
        try:
            eq = float(self.client.get_total_equity()) if self.client else 0.0
        except Exception:
            self._last_equity_record = now  # 失败退避 60s，避免打爆接口
            return
        if eq <= 0:
            self._last_equity_record = now
            return
        try:
            self.db.add_equity(int(now), round(eq, 4))
            self._last_equity_record = now
        except Exception as e:
            if getattr(self, "log", None):
                self.log.debug(f"[equity] write failed: {e}")

    def _check_risk(self, price: float, prev_price: float):
        try:
            futures_count = len([name for name in self.strategies.keys() if name.startswith("futures")])
            if futures_count > 0:
                if self.client:
                    try:
                        eq = self.client.get_total_equity()
                        self.risk.total_loss.check(eq)
                    except Exception:
                        pass
                if self.risk.total_loss.halted:
                    halt_reason = (
                        self.risk.total_loss.halt_reason or "global_total_loss"
                    )
                    if getattr(self, "risk_gate", None):
                        self.risk_gate.set_state(
                            "HALTED", halt_reason, actor="engine_risk"
                        )
                    if halt_reason == "account_data_unavailable":
                        self.log.warning(
                            "[risk] account data unavailable; block openings only"
                        )
                        self.notifier.alert(
                            "账户数据保护",
                            "OKX 暂时无法返回账户权益，系统暂停新开仓，保留减仓和止盈。",
                            key="account_data_unavailable_halt",
                        )
                        return
                    self.log.warning("[risk] global total loss halt engaged")
                    self.notifier.alert(
                        "全局亏损停机",
                        "账户确认亏损已达到全局止损线，所有策略停止交易。",
                        key="global_total_loss_halt",
                    )
                    for s in self.strategies.values():
                        s.set_enabled(False)
                    return
                gate_status = (
                    self.risk_gate.get_status()
                    if getattr(self, "risk_gate", None)
                    else {}
                )
                if (
                    getattr(self, "risk_gate", None)
                    and gate_status.get("state") == "HALTED"
                    and gate_status.get("reason") == "account_data_unavailable"
                ):
                    self.risk_gate.set_state(
                        "ACTIVE", "account_data_recovered", actor="engine_risk"
                    )
                    self.log.info(
                        "[risk] account equity recovered; opening gate restored"
                    )
                    for strategy in self.strategies.values():
                        strategy.set_enabled(True)
                for name, strategy in self.strategies.items():
                    if not name.startswith("futures"):
                        continue
                    try:
                        symbol = self._strategy_symbol(strategy)
                        sym_price = self._get_symbol_price(symbol)
                        sym_prev = self._prev_prices.get(symbol, prev_price)
                        self._check_symbol_risk(name, strategy, sym_price, sym_prev)
                    except Exception as e:
                        self.log.warning(f"[risk] {name} per-symbol check failed: {e}")
                futures = [s for n, s in self.strategies.items() if n.startswith("futures")]
                if futures and all(bool(getattr(s, "_trading_halted", False)) for s in futures):
                    gate_status = self.risk_gate.get_status() if getattr(self, "risk_gate", None) else {}
                    if getattr(self, "risk_gate", None) and (
                        gate_status.get("state") != "HALTED"
                        or gate_status.get("reason") != "all_futures_strategies_halted"
                    ):
                        self.risk_gate.set_state(
                            "HALTED", "all_futures_strategies_halted", actor="engine_risk"
                        )
                return
            risk_data = {"margin": 0, "notional": 0}
            if self.client:
                try:
                    risk_data = self.client.get_position_risk(
                        self.config.get("futures", {}).get("symbol", "BTC-USDT-SWAP")
                    ) or risk_data
                    risk_data["mark_px"] = price
                    eq = self.client.get_total_equity()
                    self.risk.total_loss.check(eq)
                except Exception:
                    pass
            lev = self.config.get("futures", {}).get("leverage", 3)
            status = self.risk.check_all(risk_data, lev, price, prev_price)
            if status.frozen or not status.safe:
                if getattr(self, "risk_gate", None):
                    self.risk_gate.set_state(
                        "HALTED",
                        self.risk.halt_reason or "risk_unsafe",
                        actor="engine_risk",
                    )
                self.log.warning(f"[风控] 已触发, reason={self.risk.halt_reason}")
                for s in self.strategies.values():
                    s.set_enabled(False)
            else:
                for s in self.strategies.values():
                    s.set_enabled(True)
        except Exception as e:
            self.log.warning(f"[风控] 检查异常: {e}")

    def _update_moca(self):
        if not self.client or not getattr(self, "moca", None):
            return
        try:
            for name, strategy in self.strategies.items():
                symbol = self._strategy_symbol(strategy)
                cfg = getattr(strategy, "_engine_config", self.config) or self.config
                tf_cfg = cfg.get("moca", {}).get("timeframes", []) or [cfg.get("global", {}).get("kline_interval", "5m")]
                limit = int(cfg.get("global", {}).get("kline_limit", 210))
                if not symbol:
                    continue
                tf_candles = {}
                for tf in tf_cfg:
                    try:
                        candles = self.client.get_candles(symbol, bar=tf, limit=limit)
                        if candles:
                            tf_candles[tf] = candles
                    except Exception:
                        continue
                if not tf_candles:
                    continue
                moca_engine = self._moca_engines.get(name) or MOCAEngine(cfg)
                self._moca_engines[name] = moca_engine
                moca_engine.analyze_multi(tf_candles)
                params = getattr(moca_engine, "current_params", None) or moca_engine.adapt()
                payload = {
                    "state": moca_engine.current_state.value,
                    "grid_density": params.grid_density,
                    "add_layers": params.add_layers,
                    "leverage": params.leverage,
                    "tp_ratio": params.tp_ratio,
                    "vol_step": params.vol_step,
                    "atr_ratio": params.atr_ratio,
                    "confidence": params.confidence,
                }
                try:
                    strategy.update_moca(payload)
                except Exception as e:
                    self.log.warning(f"[MOCA] {name} update failed: {e}")
        except Exception as e:
            self.log.warning(f"[MOCA] 更新异常: {e}")

    def stop(self):
        self.log.info("[Engine] stopping engine...")
        self._running = False
        for _nm, _s in list(getattr(self,"strategies",{}).items()):
            try: _s.stop()
            except: pass
        try:
            if self._db_run_id: self.db.stop_run(self._db_run_id)
        except: pass
        try:
            self.db.cleanup_orphan_runs("futures_hedge")
        except:
            pass
        try: self.ws_manager.stop_all()
        except: pass
        try:
            if self.command_bus:
                self.command_bus.close()
        except:
            pass
        try:
            if self.event_ledger:
                self.event_ledger.close()
        except:
            pass
        self.log.info("[Engine] engine stopped")

    def get_status(self) -> dict:
        strategies = {}
        for name, strategy in getattr(self, "strategies", {}).items():
            try:
                if hasattr(strategy, "get_status"):
                    strategies[name] = strategy.get_status()
                else:
                    strategies[name] = {"running": getattr(strategy, "_running", False)}
            except Exception as e:
                strategies[name] = {"error": str(e)}
        halted = [name for name, status in strategies.items() if status.get("halted")]
        return {
            "running": getattr(self, "_running", False),
            "paused": getattr(self, "_paused", False),
            "strategy_count": len(strategies),
            "halted_count": len(halted),
            "halted_strategies": halted,
            "reconciler": (
                self.reconciler.get_status() if getattr(self, "reconciler", None) else None
            ),
            "risk_gate": self.risk_gate.get_status() if getattr(self, "risk_gate", None) else None,
            "config_validation": getattr(self, "config_validation", {}),
            "strategies": strategies,
        }

    def _write_status_snapshot(self):
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            payload = self.get_status()
            payload["updated_at"] = int(time.time())
            with open(os.path.join(self._data_dir, "strategies_status.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            if getattr(self, "log", None):
                self.log.warning(f"[status] snapshot write failed: {e}")

    def _main_loop(self):
        interval=self.config.get("global",{}).get("tick_interval",3)
        while self._running:
            cs=time.time()
            self._consume_control_commands()
            self._record_equity()
            if self.reconciler and self.reconciler.is_due():
                self.reconciler.reconcile_all()
            if not self._paused:
                price_cache = {}
                prev_price_cache = {}
                if int(cs) % 30 < interval:
                    self._update_moca()
                primary_symbol = self.config.get("futures", {}).get("symbol", "")
                primary_price = price_cache.get(primary_symbol, 0.0)
                if primary_price <= 0 and primary_symbol:
                    try:
                        primary_price = self._get_current_price(primary_symbol)
                        price_cache[primary_symbol] = primary_price
                    except Exception:
                        primary_price = 0.0
                if primary_price > 0:
                    prev_price = prev_price_cache.get(primary_symbol, 0.0)
                    self._check_risk(primary_price, prev_price)
                for _nm,_s in list(self.strategies.items()):
                    if not getattr(_s,"is_running",True):
                        continue
                    symbol = self._strategy_symbol(_s)
                    prev_price_cache[symbol] = self._prev_prices.get(symbol, 0.0)
                    try:
                        price = price_cache.get(symbol)
                        if price is None:
                            price = self._get_current_price(symbol)
                            price_cache[symbol] = price
                    except:
                        price = 0
                    if price <= 0:
                        continue
                    try:
                        _quote = None
                        try:
                            if getattr(self, "ws_manager", None):
                                _quote = self.ws_manager.get_quote(symbol)
                        except Exception:
                            _quote = None
                        if _quote:
                            _ticker = {"symbol": symbol, "last": _quote["last"], "bid": _quote["bid"], "ask": _quote["ask"]}
                            self._ws_quote_hits = getattr(self, "_ws_quote_hits", 0) + 1
                        else:
                            _ticker = {"symbol": symbol, "last": price, "bid": price, "ask": price}
                            self._ws_quote_miss = getattr(self, "_ws_quote_miss", 0) + 1
                        _result = _s.on_tick(_ticker)
                        self._handle_strategy_result(_s, _result)
                        self._prev_prices[symbol] = price
                    except Exception as e:
                        self.log.error(f"[strategy] {_nm} err: {e}")
                if self._hb_tick % 100 == 0 and self._hb_tick > 0:
                    try:
                        _wm = getattr(self, "ws_manager", None)
                        if _wm:
                            _h = getattr(self, "_ws_quote_hits", 0)
                            _m = getattr(self, "_ws_quote_miss", 0)
                            _t = _h + _m
                            _p = (_h * 100.0 / _t) if _t else 0.0
                            _parts = " ".join(
                                f"{x['symbol']}:ok={x['ok']},age={x['age']}s,spread={x['spread']}"
                                for x in _wm.health_summary()
                            )
                            self.log.info(f"[WS-health] quote_hit={_p:.1f}% ({_h}/{_t}) {_parts}")
                            self._ws_quote_hits = 0
                            self._ws_quote_miss = 0
                    except Exception:
                        pass
                if self._hb_tick % 2 == 0:
                    self._write_status_snapshot()
                self._hb_tick += 1
            time.sleep(max(0.1,interval-(time.time()-cs)))
        self.log.info("[Engine] main loop ended")
