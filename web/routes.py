"""FastAPI Web 管理后台 - API路由"""
import os, json, sqlite3, time, shutil, re, csv
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import yaml
from config_manager import (
    load_web_config, save_web_config, load_engine_config, save_engine_config,
    test_api_connection, generate_token
)
from auth import check_token
from control.command_bus import CommandBus

router = APIRouter(prefix="/api")

PROJ = Path(__file__).resolve().parent.parent
CONFIG_HISTORY_PATH = PROJ / "data" / "config_history.jsonl"


def _enqueue_control_command(command_type: str, target: str = "", payload=None) -> str:
    bus = CommandBus(str(PROJ / "data" / "control.db"))
    try:
        return bus.enqueue(command_type, target=target, payload=payload, actor="web_admin")
    finally:
        bus.close()


def _deny_direct_control():
    raise HTTPException(409, "direct Web mutation is disabled; use versioned deployment control")


def _primary_strategy_snapshot():
    snapshot = _load_strategy_snapshot()
    strategies = snapshot.get("strategies", {}) if isinstance(snapshot, dict) else {}
    for strategy in strategies.values():
        if strategy.get("is_primary_profile"):
            return strategy
    return next(iter(strategies.values()), {})


def _projected_positions():
    db_path = PROJ / "data" / "trading.db"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT symbol,pos_side,quantity,average_price,unrealized_pnl
                FROM exchange_position_state
                WHERE CAST(quantity AS REAL) <> 0
                ORDER BY symbol,pos_side
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "symbol": row[0],
            "side": row[1],
            "qty": _safe_float_value(row[2]),
            "entry": _safe_float_value(row[3]),
            "upnl": _safe_float_value(row[4]),
        }
        for row in rows
    ]


def _projected_live_orders():
    db_path = PROJ / "data" / "trading.db"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM exchange_order_state
                WHERE state <> 'NOT_LIVE'
                ORDER BY symbol,order_id
                """
            ).fetchall()
    except sqlite3.Error:
        return []
    orders = []
    for row in rows:
        try:
            orders.append(json.loads(row[0]))
        except (TypeError, ValueError):
            continue
    return orders

# ---- 依赖：认证 ----
def require_auth(token: str = ""):
    # 从Cookie或Header获取token
    pass

# ---- 辅助函数 ----
def _get_token(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""

def _api_key_masked(key):
    if len(key) > 8:
        return key[:4] + "*" * (len(key) - 8) + key[-4:]
    return key

def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except:
        try:
            return json.loads(path.read_text())
        except:
            return {}

def _stable_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _flatten_dict(data, prefix="") -> dict:
    flat = {}
    if isinstance(data, dict):
        for key in sorted(data.keys()):
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten_dict(data[key], next_prefix))
        return flat
    if isinstance(data, list):
        flat[prefix] = json.dumps(data, ensure_ascii=False, sort_keys=True)
        return flat
    flat[prefix] = data
    return flat

def _diff_config_keys(before: dict, after: dict) -> list:
    left = _flatten_dict(before or {})
    right = _flatten_dict(after or {})
    keys = sorted(set(left.keys()) | set(right.keys()))
    return [key for key in keys if left.get(key) != right.get(key)]

def _append_config_history(scope: str, before: dict, after: dict, source: str = "panel") -> dict:
    if _stable_json(before or {}) == _stable_json(after or {}):
        return {"saved": False, "change_count": 0, "changed_keys": []}
    changed_keys = _diff_config_keys(before or {}, after or {})
    entry = {
        "id": f"{int(time.time() * 1000)}_{scope}",
        "ts": int(time.time()),
        "scope": scope,
        "source": source,
        "change_count": len(changed_keys),
        "changed_keys": changed_keys[:40],
        "before": before or {},
        "after": after or {},
    }
    os.makedirs(CONFIG_HISTORY_PATH.parent, exist_ok=True)
    with open(CONFIG_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"saved": True, "change_count": len(changed_keys), "changed_keys": changed_keys[:40], "entry": entry}

def _read_config_history(limit: int = 20) -> list:
    if not CONFIG_HISTORY_PATH.exists():
        return []
    items = []
    with open(CONFIG_HISTORY_PATH, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except:
                continue
    items.sort(key=lambda item: item.get("ts", 0), reverse=True)
    return items[:max(1, min(limit, 100))]

def _merge_config_sections(base: dict, patch: dict) -> dict:
    merged = json.loads(json.dumps(base))
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged

def _load_execution_rows(limit: int = 500) -> list:
    csv_path = PROJ / "data" / "pnl_records.csv"
    if not csv_path.exists():
        return []
    rows = []
    with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows[-max(1, min(limit, 5000)):]

def _build_execution_metrics(limit: int = 500) -> dict:
    rows = _load_execution_rows(limit)
    if not rows:
        return {
            "available": False,
            "total_closed_trades": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "total_fees": 0.0,
            "net_pnl": 0.0,
            "avg_net": 0.0,
            "avg_fee": 0.0,
            "profit_factor": 0.0,
            "fee_ratio_pct": 0.0,
            "recent_24h_trades": 0,
            "recent_24h_net": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "by_side": {},
            "recent": [],
        }
    total = len(rows)
    wins = 0
    gross_pnl = 0.0
    total_fees = 0.0
    net_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    best_trade = None
    worst_trade = None
    side_stats = {}
    cutoff = time.time() - 86400
    recent_24h_trades = 0
    recent_24h_net = 0.0
    recent = []
    for row in rows:
        gross = float(row.get("gross", 0) or 0)
        fee = float(row.get("fee", 0) or 0)
        net = float(row.get("net", 0) or 0)
        side = row.get("side", "") or "unknown"
        side_entry = side_stats.setdefault(side, {"count": 0, "net": 0.0, "wins": 0})
        side_entry["count"] += 1
        side_entry["net"] += net
        if net > 0:
            wins += 1
            gross_profit += net
            side_entry["wins"] += 1
        elif net < 0:
            gross_loss += abs(net)
        gross_pnl += gross
        total_fees += fee
        net_pnl += net
        best_trade = net if best_trade is None else max(best_trade, net)
        worst_trade = net if worst_trade is None else min(worst_trade, net)
        ts_raw = row.get("time", "")
        ts_value = 0
        if ts_raw:
            try:
                ts_value = int(time.mktime(time.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")))
            except:
                ts_value = 0
        if ts_value >= cutoff:
            recent_24h_trades += 1
            recent_24h_net += net
        recent.append({
            "time": ts_raw,
            "symbol": row.get("symbol", ""),
            "side": side,
            "qty": float(row.get("qty", 0) or 0),
            "entry": float(row.get("entry", 0) or 0),
            "exit": float(row.get("exit", 0) or 0),
            "gross": round(gross, 4),
            "fee": round(fee, 4),
            "net": round(net, 4),
        })
    by_side = {}
    for side, entry in side_stats.items():
        count = entry["count"] or 1
        by_side[side] = {
            "count": entry["count"],
            "net": round(entry["net"], 4),
            "win_rate": round(entry["wins"] / count * 100, 2),
        }
    fee_ratio_pct = (total_fees / abs(gross_pnl) * 100) if abs(gross_pnl) > 1e-9 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-9 else (gross_profit if gross_profit > 0 else 0.0)
    return {
        "available": True,
        "total_closed_trades": total,
        "win_rate": round(wins / total * 100, 2),
        "gross_pnl": round(gross_pnl, 4),
        "total_fees": round(total_fees, 4),
        "net_pnl": round(net_pnl, 4),
        "avg_net": round(net_pnl / total, 4),
        "avg_fee": round(total_fees / total, 4),
        "profit_factor": round(profit_factor, 4),
        "fee_ratio_pct": round(fee_ratio_pct, 2),
        "recent_24h_trades": recent_24h_trades,
        "recent_24h_net": round(recent_24h_net, 4),
        "best_trade": round(best_trade or 0.0, 4),
        "worst_trade": round(worst_trade or 0.0, 4),
        "by_side": by_side,
        "recent": recent[-10:][::-1],
    }

def _file_health(rel_path: str, warn_after: float, critical_after: float, allow_empty: bool = False, label: str = "") -> dict:
    path = PROJ / rel_path
    item = {
        "name": label or Path(rel_path).name,
        "path": rel_path,
        "exists": path.exists(),
        "status": "missing",
        "age_sec": None,
        "updated_at": 0,
        "size": 0,
        "note": "",
    }
    if not path.exists():
        item["note"] = "missing"
        return item
    stat = path.stat()
    age_sec = max(0.0, time.time() - stat.st_mtime)
    item["age_sec"] = round(age_sec, 1)
    item["updated_at"] = int(stat.st_mtime)
    item["size"] = int(stat.st_size)
    if stat.st_size == 0:
        item["status"] = "ok" if allow_empty else "warn"
        item["note"] = "empty"
        return item
    if age_sec >= critical_after:
        item["status"] = "critical"
        item["note"] = "stale"
    elif age_sec >= warn_after:
        item["status"] = "warn"
        item["note"] = "aging"
    else:
        item["status"] = "ok"
        item["note"] = "fresh"
    return item


def _latest_file_health(paths, warn_after: float, critical_after: float, allow_empty: bool = False, label: str = "") -> tuple[dict, list[dict]]:
    """Return aggregate freshness from the newest member and all member details."""
    members = [
        _file_health(rel_path, warn_after, critical_after, allow_empty=allow_empty, label=member_label)
        for rel_path, member_label in paths
    ]
    existing = [item for item in members if item["exists"]]
    if not existing:
        aggregate = {
            "name": label or "records",
            "path": [rel_path for rel_path, _member_label in paths],
            "exists": False,
            "status": "missing",
            "age_sec": None,
            "updated_at": 0,
            "size": 0,
            "note": "missing",
        }
    else:
        newest = max(existing, key=lambda item: item["updated_at"])
        aggregate = dict(newest)
        aggregate["name"] = label or newest["name"]
        aggregate["path"] = [rel_path for rel_path, _member_label in paths]
        aggregate["size"] = sum(item["size"] for item in existing)
        aggregate["note"] = f"{newest['note']}:{Path(str(newest['path'])).name}"
    aggregate["members"] = members
    return aggregate, members

def _safe_float_value(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except:
        return default

def _current_engine_pid() -> int:
    pid_file = PROJ / "data" / "run.pid"
    if not pid_file.exists():
        return 0
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except:
        return 0

def _okx_fill_id(fill: dict) -> str:
    return str(
        fill.get("tradeId")
        or fill.get("billId")
        or f"{fill.get('ordId', '')}:{fill.get('side', '')}:{fill.get('posSide', '')}:{fill.get('fillPx', '')}:{fill.get('fillSz', '')}:{fill.get('ts', '')}"
    )

def _build_recent_log_health(limit: int = 200) -> dict:
    log_path = PROJ / "logs" / "trading.log"
    result = {
        "warnings": 0,
        "errors": 0,
        "last_warning": "",
        "last_error": "",
    }
    if not log_path.exists():
        return result
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max(20, min(limit, 1000)):]
    except:
        return result
    for raw in lines:
        line = raw.strip()
        upper = line.upper()
        is_error = "[ERROR]" in upper or upper.startswith("ERROR") or "TRACEBACK" in upper or "EXCEPTION" in upper
        is_warn = "[WARNING]" in upper or " WARN" in upper or "WARNING" in upper
        if is_error:
            result["errors"] += 1
            result["last_error"] = result["last_error"] or line
        elif is_warn:
            result["warnings"] += 1
            result["last_warning"] = result["last_warning"] or line
    return result



def _clean_api_error(exc) -> str:
    return f"{type(exc).__name__}"


def _build_public_okx_client():
    from api.okx_client import OKXClient
    return OKXClient({"api_key": "", "api_secret": "", "passphrase": ""}, flag="1")


def _check_api_key_permissions(api_key: str, api_secret: str, passphrase: str) -> dict:
    from api.okx_client import OKXClient
    try:
        client = OKXClient({"api_key": api_key, "api_secret": api_secret, "passphrase": passphrase}, flag="0")
        result = client.check_api_key_permissions()
    except Exception as exc:
        result = {"ok": False, "can_read_balance": False, "risk_level": "high", "msg": f"API Key 检测失败: {_clean_api_error(exc)}", "warnings": []}
    warnings = list(result.get("warnings") or [])
    warnings.append("客户策略将使用实盘模式，请确认 API 无提币权限并控制资金规模")
    result["warnings"] = warnings
    return result


def _symbol_admission_payload(symbol: str, initial_margin_usdt=0, leverage=3) -> dict:
    warnings = []
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"ok": False, "msg": "symbol required", "warnings": warnings}
    try:
        client = _build_public_okx_client()
        info = client.get_futures_info(symbol)
        ticker = client.get_ticker(symbol)
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "msg": f"币种检查失败: {_clean_api_error(exc)}", "warnings": warnings}
    if not info.get("ok", True) or not info.get("ctVal"):
        return {"ok": False, "symbol": symbol, "market": info, "msg": "币种不存在或合约参数不可用", "warnings": warnings}
    last = float(ticker.get("last") or 0)
    min_sz = float(info.get("minSz") or 0)
    ct_val = float(info.get("ctVal") or 0)
    lever = max(1.0, float(leverage or 1))
    min_notional = min_sz * ct_val * last if last > 0 else 0.0
    min_margin = min_notional / lever if lever > 0 else min_notional
    if initial_margin_usdt and min_margin and float(initial_margin_usdt) < min_margin * 1.2:
        warnings.append("资金低于最小合约保证金的 1.2x 缓冲，建议提高单策略资金")
    return {
        "ok": True,
        "symbol": symbol,
        "market": info,
        "ticker": {"last": last},
        "min_notional_usdt": round(min_notional, 6),
        "estimated_min_margin_usdt": round(min_margin, 6),
        "warnings": warnings,
    }


def _strategy_precheck_payload(data: dict, key_data: dict = None) -> dict:
    warnings = []
    symbol = (data.get("symbol") or "").strip().upper()
    leverage = int(data.get("leverage", 3) or 3)
    capital_budget = float(data.get("capital_budget_usdt", data.get("total_capital_usdt", 0)) or 0)
    base_position = float(data.get("base_position_usdt", data.get("initial_margin_usdt", 5)) or 5)
    paper_mode = False
    if capital_budget > 0:
        base_position = min(base_position, max(1.0, capital_budget * 0.25))
    admission = _symbol_admission_payload(symbol, base_position, leverage)
    if not admission.get("ok"):
        return {"ok": False, "paper_mode": paper_mode, "symbol_admission": admission, "warnings": admission.get("warnings", [])}
    warnings.extend(admission.get("warnings", []))
    balance = None
    max_margin_ratio = max(0.1, min(float(data.get("max_margin_ratio", 0.5) or 0.5), 0.8))
    required_balance = max(
        base_position * 1.2,
        admission.get("estimated_min_margin_usdt", 0) * 1.2,
        base_position / max_margin_ratio,
        capital_budget if capital_budget > 0 else 0,
    )
    funds_enough = False
    if key_data and key_data.get("ok"):
        try:
            from api.okx_client import OKXClient
            client = OKXClient({"api_key": key_data["api_key"], "api_secret": key_data["api_secret"], "passphrase": key_data["passphrase"]}, flag="0")
            balance = client.get_balance("USDT")
            funds_enough = balance >= required_balance
            if not funds_enough:
                warnings.append("账户可用 USDT 低于建议资金缓冲，不建议启动实盘")
        except Exception:
            warnings.append("余额读取失败，请确认 API 权限后再启动实盘")
    warnings.append("客户策略为实盘模式，请确认风险后启动")
    recommended_base = max(base_position, admission.get("estimated_min_margin_usdt", 0) * 1.5, 5)
    return {
        "ok": bool(funds_enough),
        "paper_mode": paper_mode,
        "funds_enough": bool(funds_enough),
        "capital_budget_usdt": round(capital_budget, 2),
        "min_contract_suitable": not bool(admission.get("warnings")),
        "balance_usdt": balance,
        "required_balance_usdt": round(required_balance, 2),
        "symbol_admission": admission,
        "recommended_params": {
            "leverage": min(max(leverage, 1), 5),
            "base_position_usdt": round(recommended_base, 2),
            "max_add_layers": int(data.get("max_add_layers", 6) or 6),
            "add_step": float(data.get("add_step", 0.003) or 0.003),
            "first_tp_ratio": float(data.get("first_tp_ratio", 0.0065) or 0.0065),
        },
        "warnings": warnings,
    }

def _build_ops_health() -> dict:
    pid_file = PROJ / "data" / "run.pid"
    pid = None
    running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except:
            pid = None
    if pid:
        try:
            os.kill(pid, 0)
            running = True
        except:
            running = False

    execution_records, execution_record_details = _latest_file_health(
        [
            ("data/pnl_records.csv", "execution_records:CRV"),
            ("data/pnl_records_dram.csv", "execution_records:DRAM"),
            ("data/pnl_records_fartcoin.csv", "execution_records:FARTCOIN"),
        ],
        warn_after=86400,
        critical_after=604800,
        allow_empty=True,
        label="execution_records",
    )
    for item in execution_record_details:
        item["affects_global_health"] = False

    files = [
        _file_health("data/strategies_status.json", warn_after=15, critical_after=45, label="strategy_snapshot"),
        _file_health("data/fee_pnl.json", warn_after=120, critical_after=360, label="pnl_heartbeat"),
        _file_health("logs/trading.log", warn_after=90, critical_after=300, label="trading_log"),
        execution_records,
        *execution_record_details,
    ]
    recent_logs = _build_recent_log_health()
    snapshot = _load_strategy_snapshot()
    strategies = snapshot.get("strategies", {}) if isinstance(snapshot, dict) else {}
    halted = []
    for name, status in strategies.items():
        if isinstance(status, dict) and status.get("halted"):
            halted.append(name)
    global_health_files = [item for item in files if item.get("affects_global_health", True)]
    critical_count = sum(1 for item in global_health_files if item["status"] == "critical")
    warn_count = sum(1 for item in global_health_files if item["status"] == "warn")
    if not running:
        critical_count += 1
    if recent_logs["errors"] > 0:
        warn_count += 1
    status = "ok"
    if critical_count > 0:
        status = "critical"
    elif warn_count > 0 or halted:
        status = "warn"
    score = max(0, 100 - critical_count * 35 - warn_count * 12 - min(len(halted), 2) * 8)
    stale = [item["name"] for item in global_health_files if item["status"] in ("warn", "critical", "missing")]
    return {
        "status": status,
        "score": score,
        "engine_running": running,
        "pid": pid,
        "strategy_count": len(strategies),
        "halted_strategies": halted,
        "warning_count": warn_count,
        "critical_count": critical_count,
        "stale_components": stale,
        "files": files,
        "logs": recent_logs,
        "snapshot_updated_at": snapshot.get("updated_at", 0) if isinstance(snapshot, dict) else 0,
    }

def _profile_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return slug[:48] or "strategy"

def _profile_runtime_name(symbol: str) -> str:
    base = (symbol or "").split("-")[0].strip().lower()
    return f"futures_{_profile_slug(base or 'extra')}"

def _profile_enabled_path(slug: str) -> Path:
    return PROJ / "config" / f"config_{slug}.yaml"

def _profile_disabled_path(slug: str) -> Path:
    return PROJ / "config" / f"disabled_config_{slug}.yaml"

def _profile_find_path(slug: str):
    enabled = _profile_enabled_path(slug)
    disabled = _profile_disabled_path(slug)
    if enabled.exists():
        return enabled, True
    if disabled.exists():
        return disabled, False
    return None, False

def _load_yaml_file(path: Path) -> dict:
    if not path or not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except:
        return {}

def _write_yaml_file(path: Path, data: dict):
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

def _build_profile_config(base_config: dict, payload: dict, slug: str, is_base: bool = False, prior: dict = None) -> dict:
    cfg = json.loads(json.dumps(base_config or {}))
    prior = prior or {}
    futures = cfg.setdefault("futures", {})
    global_cfg = cfg.setdefault("global", {})
    profile_meta = prior.get("profile", {}) if isinstance(prior.get("profile", {}), dict) else {}
    profile_meta.update({
        "slug": "base" if is_base else slug,
        "name": (payload.get("name") or profile_meta.get("name") or futures.get("symbol") or slug),
        "symbol": payload.get("symbol") or futures.get("symbol", ""),
        "runtime_name": "futures" if is_base else _profile_runtime_name(payload.get("symbol") or futures.get("symbol", "")),
        "updated_at": int(time.time()),
    })
    if not profile_meta.get("created_at"):
        profile_meta["created_at"] = int(time.time())
    if payload.get("symbol"):
        futures["symbol"] = payload.get("symbol")
    field_map = {
        "leverage": ("futures", int),
        "base_position_usdt": ("futures", float),
        "max_add_layers": ("futures", int),
        "add_step": ("futures", float),
        "add_multiplier": ("futures", float),
        "first_tp_ratio": ("futures", float),
        "final_tp_ratio": ("futures", float),
        "max_margin_ratio": ("futures", float),
        "tick_interval": ("global", float),
    }
    for key, (section, caster) in field_map.items():
        if key not in payload or payload.get(key) in ("", None):
            continue
        target = futures if section == "futures" else global_cfg
        try:
            target[key] = caster(payload.get(key))
        except:
            continue
    cfg["profile"] = profile_meta
    return cfg

def _profile_summary(slug: str, cfg: dict, enabled: bool, is_base: bool, runtime_status: dict) -> dict:
    profile_meta = cfg.get("profile", {}) if isinstance(cfg.get("profile", {}), dict) else {}
    futures = cfg.get("futures", {}) if isinstance(cfg.get("futures", {}), dict) else {}
    global_cfg = cfg.get("global", {}) if isinstance(cfg.get("global", {}), dict) else {}
    runtime_name = "futures" if is_base else profile_meta.get("runtime_name") or _profile_runtime_name(futures.get("symbol", ""))
    loaded = bool(runtime_status)
    running = bool(runtime_status.get("running")) if isinstance(runtime_status, dict) else False
    return {
        "slug": slug,
        "name": profile_meta.get("name") or futures.get("symbol") or slug,
        "symbol": futures.get("symbol", ""),
        "runtime_name": runtime_name,
        "enabled": enabled,
        "is_base": is_base,
        "loaded": loaded,
        "running": running,
        "halted": bool(runtime_status.get("halted")) if isinstance(runtime_status, dict) else False,
        "halt_reason": runtime_status.get("halt_reason", "") if isinstance(runtime_status, dict) else "",
        "restart_required": (enabled and not loaded) or ((not enabled) and loaded),
        "editable": True,
        "toggleable": not is_base,
        "deletable": not is_base,
        "config": {
            "symbol": futures.get("symbol", ""),
            "leverage": futures.get("leverage", 0),
            "base_position_usdt": futures.get("base_position_usdt", 0),
            "max_add_layers": futures.get("max_add_layers", 0),
            "add_step": futures.get("add_step", 0),
            "add_multiplier": futures.get("add_multiplier", 0),
            "first_tp_ratio": futures.get("first_tp_ratio", 0),
            "final_tp_ratio": futures.get("final_tp_ratio", 0),
            "max_margin_ratio": futures.get("max_margin_ratio", 0),
            "tick_interval": global_cfg.get("tick_interval", 0),
        },
        "runtime": runtime_status or {},
        "updated_at": profile_meta.get("updated_at", 0),
        "created_at": profile_meta.get("created_at", 0),
        "file_name": "" if is_base else (_profile_enabled_path(slug).name if enabled else _profile_disabled_path(slug).name),
    }

def _list_engine_profiles() -> dict:
    snapshot = _load_strategy_snapshot()
    runtime_map = snapshot.get("strategies", {}) if isinstance(snapshot, dict) else {}
    profiles = []
    base_cfg = load_engine_config()
    profiles.append(_profile_summary("base", base_cfg, True, True, runtime_map.get("futures", {})))
    seen = {"base"}
    config_dir = PROJ / "config"
    for path in sorted(config_dir.glob("config_*.yaml")) + sorted(config_dir.glob("disabled_config_*.yaml")):
        name = path.name
        enabled = name.startswith("config_")
        slug = name[len("config_"):-5] if enabled else name[len("disabled_config_"):-5]
        if slug in seen:
            continue
        seen.add(slug)
        cfg = _load_yaml_file(path)
        runtime_name = (_load_yaml_file(path).get("profile", {}) or {}).get("runtime_name") or _profile_runtime_name(cfg.get("futures", {}).get("symbol", ""))
        profiles.append(_profile_summary(slug, cfg, enabled, False, runtime_map.get(runtime_name, {})))
    profiles.sort(key=lambda item: (0 if item["is_base"] else 1, item["name"].lower()))
    return {
        "profiles": profiles,
        "engine": {
            "running": bool(snapshot.get("running")) if isinstance(snapshot, dict) else False,
            "strategy_count": len(runtime_map) if isinstance(runtime_map, dict) else 0,
            "updated_at": snapshot.get("updated_at", 0) if isinstance(snapshot, dict) else 0,
        },
    }

def _ensure_profile_uniqueness(slug: str, symbol: str, current_slug: str = ""):
    runtime_name = _profile_runtime_name(symbol)
    listing = _list_engine_profiles()
    for item in listing.get("profiles", []):
        if item.get("slug") == current_slug:
            continue
        if item.get("slug") == slug:
            raise HTTPException(400, f"profile slug already exists: {slug}")
        if (item.get("symbol") or "").upper() == (symbol or "").upper():
            raise HTTPException(400, f"symbol already exists: {symbol}")
        if item.get("runtime_name") == runtime_name:
            raise HTTPException(400, f"symbol prefix already in use: {symbol}")


def _strategy_pnl_summary(snapshot: dict) -> tuple[dict, dict]:
    """Aggregate independent strategy ledgers for account-level display only."""
    data_dir = (PROJ / "data").resolve()
    strategies = snapshot.get("strategies", {}) if isinstance(snapshot, dict) else {}
    summary = {
        "total_pnl": 0.0,
        "session_pnl": 0.0,
        "daily_pnl": 0.0,
        "total_fees": 0.0,
        "total_funding": 0.0,
    }
    by_strategy = {}
    seen_paths = set()

    for runtime, status in strategies.items():
        if not isinstance(status, dict):
            continue
        fee_pnl_file = str(status.get("fee_pnl_file", "") or "")
        if not fee_pnl_file:
            continue
        path = (PROJ / fee_pnl_file).resolve()
        # The runtime status is local state, but it must never direct this API
        # to read outside the accounting data directory.
        if path.parent != data_dir or not path.name.startswith("fee_pnl") or path in seen_paths:
            continue
        seen_paths.add(path)
        pnl_data = _load_json_file(path)
        if not pnl_data:
            continue
        total_pnl = _safe_float_value(pnl_data.get("total_realized_pnl", 0))
        session_pnl = total_pnl - _safe_float_value(pnl_data.get("session_start_total_pnl", total_pnl))
        daily_pnl = total_pnl - _safe_float_value(pnl_data.get("day_start_total_pnl", total_pnl))
        total_fees = _safe_float_value(pnl_data.get("total_fees_paid", 0))
        total_funding = _safe_float_value(pnl_data.get("total_funding_paid", 0))
        by_strategy[str(runtime)] = {
            "symbol": status.get("symbol", ""),
            "realized_pnl": round(total_pnl, 2),
            "session_pnl": round(session_pnl, 2),
            "daily_pnl": round(daily_pnl, 2),
            "total_fees": round(total_fees, 4),
            "total_funding": round(total_funding, 4),
        }
        summary["total_pnl"] += total_pnl
        summary["session_pnl"] += session_pnl
        summary["daily_pnl"] += daily_pnl
        summary["total_fees"] += total_fees
        summary["total_funding"] += total_funding

    return summary, by_strategy


def _build_risk_snapshot(balance: float = 0.0) -> dict:
    cfg = load_engine_config()
    snapshot = _load_strategy_snapshot()
    primary = _primary_strategy_snapshot()
    balance = _safe_float_value(primary.get("cached_equity", 0))
    fc = cfg.get("futures", {})
    pnl_data = _load_json_file(PROJ / "data" / "fee_pnl.json")
    pnl_summary, pnl_by_strategy = _strategy_pnl_summary(snapshot)
    halt_state = _load_json_file(PROJ / "data" / "halt_state.json")
    initial_equity = float(pnl_data.get("initial_equity", 0) or 0)
    risk_equity_baseline = float(pnl_data.get("risk_equity_baseline", initial_equity) or initial_equity or 0)
    total_pnl = float(pnl_data.get("total_realized_pnl", 0) or 0)
    session_start_total_pnl = float(pnl_data.get("session_start_total_pnl", total_pnl) or total_pnl)
    session_pnl = total_pnl - session_start_total_pnl
    day_start_total_pnl = float(pnl_data.get("day_start_total_pnl", total_pnl) or total_pnl)
    daily_pnl = total_pnl - day_start_total_pnl
    day_equity_baseline = float(pnl_data.get("day_equity_baseline", risk_equity_baseline) or risk_equity_baseline or 0)
    daily_stop_until = float(pnl_data.get("daily_stop_until", 0) or 0)
    daily_stop_reason = pnl_data.get("daily_stop_reason", "")
    total_fees = float(pnl_data.get("total_fees_paid", 0) or 0)
    total_funding = float(pnl_data.get("total_funding_paid", 0) or 0)
    display_total_pnl = pnl_summary["total_pnl"] if pnl_by_strategy else total_pnl
    display_session_pnl = pnl_summary["session_pnl"] if pnl_by_strategy else session_pnl
    display_daily_pnl = pnl_summary["daily_pnl"] if pnl_by_strategy else daily_pnl
    display_total_fees = pnl_summary["total_fees"] if pnl_by_strategy else total_fees
    display_total_funding = pnl_summary["total_funding"] if pnl_by_strategy else total_funding
    max_loss_usdt = float(fc.get("max_loss_usdt", 20) or 20)
    max_drawdown = float(fc.get("max_drawdown", 0.15) or 0.15)
    daily_loss_limit_usdt = float(fc.get("daily_loss_limit_usdt", max(8, max_loss_usdt * 0.8)) or max(8, max_loss_usdt * 0.8))
    daily_drawdown_limit = float(fc.get("daily_drawdown", min(max_drawdown, 0.06)) or min(max_drawdown, 0.06))
    drawdown_ratio = 0.0
    if risk_equity_baseline > 0 and balance > 0:
        drawdown_ratio = max(0.0, (risk_equity_baseline - balance) / risk_equity_baseline)
    daily_drawdown_ratio = 0.0
    if day_equity_baseline > 0 and balance > 0:
        daily_drawdown_ratio = max(0.0, (day_equity_baseline - balance) / day_equity_baseline)
    total_loss_hit = session_pnl < -max_loss_usdt
    drawdown_hit = drawdown_ratio > max_drawdown if risk_equity_baseline > 0 and balance > 0 else False
    halted = bool(halt_state.get("halted", False))
    halt_reason = halt_state.get("reason", "") if halted else ""
    return {
        "halted": halted,
        "halt_reason": halt_reason,
        "halt_updated_at": halt_state.get("updated_at", "") if halted else "",
        "hard_stop_active": halted or total_loss_hit or drawdown_hit,
        "realized_pnl": round(display_total_pnl, 2),
        "session_pnl": round(display_session_pnl, 2),
        "daily_pnl": round(display_daily_pnl, 2),
        "total_fees": round(display_total_fees, 4),
        "total_funding": round(display_total_funding, 4),
        "pnl_by_strategy": pnl_by_strategy,
        "primary_realized_pnl": round(total_pnl, 2),
        "primary_session_pnl": round(session_pnl, 2),
        "primary_daily_pnl": round(daily_pnl, 2),
        "initial_equity": round(initial_equity, 2),
        "risk_equity_baseline": round(risk_equity_baseline, 2),
        "day_equity_baseline": round(day_equity_baseline, 2),
        "current_equity": round(balance, 2),
        "drawdown_ratio": round(drawdown_ratio, 4),
        "drawdown_pct": round(drawdown_ratio * 100, 2),
        "daily_drawdown_ratio": round(daily_drawdown_ratio, 4),
        "daily_drawdown_pct": round(daily_drawdown_ratio * 100, 2),
        "max_loss_usdt": round(max_loss_usdt, 2),
        "max_drawdown_ratio": round(max_drawdown, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "daily_loss_limit_usdt": round(daily_loss_limit_usdt, 2),
        "daily_drawdown_limit": round(daily_drawdown_limit, 4),
        "daily_stop_active": daily_stop_until > time.time(),
        "daily_stop_reason": daily_stop_reason,
        "triggered_by_total_loss": total_loss_hit,
        "triggered_by_drawdown": drawdown_hit,
        "liquidation": {
            "liq_px_unavailable": bool((primary.get("monitor", {}) or {}).get("liq_px_unavailable", False)),
            "liquidation_distance_safe": bool((primary.get("monitor", {}) or {}).get("liquidation_distance_safe", False)),
            "liquidation_warning": bool((primary.get("monitor", {}) or {}).get("liquidation_warning", False)),
            "liquidation_emergency": bool((primary.get("monitor", {}) or {}).get("liquidation_emergency", False)),
            "zone": (primary.get("monitor", {}) or {}).get("liquidation_zone", "unavailable"),
            "reason": (primary.get("monitor", {}) or {}).get("liquidation_reason", ""),
            "min_distance_rate": (primary.get("monitor", {}) or {}).get("liquidation_min_distance_rate"),
            "mark_px": (primary.get("monitor", {}) or {}).get("liquidation_mark_px"),
            "opening_pause_remaining_sec": (primary.get("monitor", {}) or {}).get("liquidation_opening_pause_remaining_sec", 0.0),
        },
    }

def _load_strategy_snapshot() -> dict:
    return _load_json_file(PROJ / "data" / "strategies_status.json")

def _report_updated_at(path: Path, data: dict) -> int:
    for key, scale in (("generated_at", 1), ("generated_at_ms", 1000)):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, (int, float)) and value > 0:
            return int(value / scale)
    captured_at = data.get("captured_at") if isinstance(data, dict) else None
    if captured_at:
        try:
            return int(datetime.fromisoformat(str(captured_at).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            pass
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0

def _report_market_closed(data: dict) -> bool:
    quotes = data.get("quotes", {}) if isinstance(data, dict) else {}
    quote_rows = quotes.values() if isinstance(quotes, dict) else quotes if isinstance(quotes, list) else []
    if any(str(row.get("market_status", "")).lower() == "closed" for row in quote_rows if isinstance(row, dict)):
        return True
    reasons = []
    for theme in data.get("themes", []) if isinstance(data, dict) else []:
        reasons.extend(theme.get("reasons", []) if isinstance(theme, dict) else [])
    signal = data.get("signal", {}) if isinstance(data, dict) else {}
    if isinstance(signal, dict):
        reasons.extend(signal.get("reasons", []))
    return any("market_closed" in str(reason) for reason in reasons)

def _radar_report(latest_name: str, forward_name: str, now: int, inventory_name: str = "") -> dict:
    data_dir = PROJ / "data"
    latest_path = data_dir / latest_name
    latest = _load_json_file(latest_path)
    updated_at = _report_updated_at(latest_path, latest)
    age_sec = max(0, now - updated_at) if updated_at else 0
    if not latest:
        freshness = "missing"
    elif _report_market_closed(latest):
        freshness = "closed"
    else:
        freshness = "fresh" if age_sec <= 900 else "stale"
    result = {
        "monitor_only": True,
        "latest": latest,
        "forward": _load_json_file(data_dir / forward_name),
        "updated_at": updated_at,
        "age_sec": age_sec,
        "freshness": freshness,
    }
    if inventory_name:
        inventory_path = data_dir / inventory_name
        inventory = _load_json_file(inventory_path)
        inventory_updated_at = _report_updated_at(inventory_path, inventory)
        inventory_age_sec = max(0, now - inventory_updated_at) if inventory_updated_at else 0
        result.update({
            "inventory": inventory,
            "inventory_updated_at": inventory_updated_at,
            "inventory_age_sec": inventory_age_sec,
            "inventory_freshness": "missing" if not inventory else freshness if freshness == "closed" else "fresh" if inventory_age_sec <= 900 else "stale",
        })
    return result

def _build_market_observability(now: int = None) -> dict:
    now = int(now or time.time())
    snapshot = _load_strategy_snapshot()
    updated_at = int(snapshot.get("updated_at", 0) or 0)
    age_sec = max(0, now - updated_at) if updated_at else 0
    moca = []
    for status in (snapshot.get("strategies", {}) or {}).values():
        monitor = status.get("monitor", {}) if isinstance(status, dict) else {}
        symbol = status.get("symbol", "") if isinstance(status, dict) else ""
        if not symbol or "moca_observed_state" not in monitor:
            continue
        moca.append({
            "symbol": symbol,
            "state": monitor.get("moca_observed_state", "unknown"),
            "confidence": float(monitor.get("moca_observed_confidence", 0) or 0),
            "apply_live": bool(monitor.get("moca_apply_live", False)),
            "updated_at": updated_at,
            "age_sec": age_sec,
            "freshness": "fresh" if updated_at and age_sec <= 30 else "stale" if updated_at else "missing",
        })
    return {
        "code": 0,
        "generated_at": now,
        "moca": moca,
        "radars": {
            "cross_market": _radar_report(
                "cross_market_opportunity_report.json",
                "cross_market_forward_report.json",
                now,
            ),
            "dram": _radar_report(
                "dram_cross_market_report.json",
                "dram_weighted_forward_report.json",
                now,
                "dram_inventory_shadow_report.json",
            ),
        },
    }

def _get_primary_strategy_status(snapshot: dict) -> dict:
    strategies = snapshot.get("strategies", {}) if isinstance(snapshot, dict) else {}
    if "futures" in strategies:
        return strategies.get("futures", {})
    if strategies:
        first_key = next(iter(strategies.keys()))
        return strategies.get(first_key, {})
    return {}



def require_admin_token(token: str):
    from auth import check_token, is_admin_token
    if not check_token(token) or not is_admin_token(token):
        raise HTTPException(403, "admin required")


def _build_monitor_snapshot(snapshot: dict, balance: float = 0.0) -> dict:
    status = _get_primary_strategy_status(snapshot)
    monitor = status.get("monitor", {}) if isinstance(status, dict) else {}
    tp_orders = int(monitor.get("tp_order_count", status.get("tp_active", 0)) or 0)
    grid_long = int(monitor.get("grid_long_count", 0) or 0)
    grid_short = int(monitor.get("grid_short_count", 0) or 0)
    total_fees = float(status.get("total_fees", 0) or 0)
    session_pnl = float(status.get("session_pnl", 0) or 0)
    realized_pnl = float(status.get("pnl", 0) or 0)
    fee_load = 0.0
    if abs(realized_pnl) > 0.0001:
        fee_load = abs(total_fees / realized_pnl)
    return {
        "symbol": status.get("symbol", ""),
        "price": monitor.get("price", 0.0),
        "long_qty": status.get("long_qty", 0.0),
        "short_qty": status.get("short_qty", 0.0),
        "long_price": status.get("long_price", 0.0),
        "short_price": status.get("short_price", 0.0),
        "long_adds": status.get("long_adds", 0),
        "short_adds": status.get("short_adds", 0),
        "imbalance_pct": monitor.get("imbalance_pct", 0.0),
        "dominant_side": monitor.get("dominant_side", "balanced"),
        "net_exposure_units": monitor.get("net_exposure_units", 0.0),
        "net_exposure_usdt": monitor.get("net_exposure_usdt", 0.0),
        "grid_long_count": grid_long,
        "grid_short_count": grid_short,
        "tp_order_count": tp_orders,
        "pending_order_estimate": grid_long + grid_short + tp_orders,
        "startup_recovery_active": monitor.get("startup_recovery_active", False),
        "startup_recovery_remaining_sec": monitor.get("startup_recovery_remaining_sec", 0.0),
        "post_recovery_refill_delay_active": monitor.get("post_recovery_refill_delay_active", False),
        "recovery_mode": monitor.get("recovery_mode", False),
        "recovery_remaining_sec": monitor.get("recovery_remaining_sec", 0.0),
        "target_grid_layers": monitor.get("target_grid_layers", 0),
        "grid_layer_limit_reason": monitor.get("grid_layer_limit_reason", ""),
        "grid_long_enabled": monitor.get("grid_long_enabled", True),
        "grid_short_enabled": monitor.get("grid_short_enabled", True),
        "liq_px_unavailable": bool(monitor.get("liq_px_unavailable", False)),
        "liquidation_distance_safe": bool(monitor.get("liquidation_distance_safe", False)),
        "liquidation_warning": bool(monitor.get("liquidation_warning", False)),
        "liquidation_emergency": bool(monitor.get("liquidation_emergency", False)),
        "liquidation_zone": monitor.get("liquidation_zone", "unavailable"),
        "liquidation_reason": monitor.get("liquidation_reason", ""),
        "liquidation_min_distance_rate": monitor.get("liquidation_min_distance_rate"),
        "liquidation_mark_px": monitor.get("liquidation_mark_px"),
        "liquidation_opening_pause_remaining_sec": monitor.get("liquidation_opening_pause_remaining_sec", 0.0),
        "grid_long_block_reason": monitor.get("grid_long_block_reason", ""),
        "grid_short_block_reason": monitor.get("grid_short_block_reason", ""),
        "opening_guard_stats": monitor.get("opening_guard_stats", {}),
        "inventory_repair_ready": monitor.get("inventory_repair_ready", monitor.get("inventory_repair_active", False)),
        "inventory_repair_reason": monitor.get("inventory_repair_reason", ""),
        "inventory_repair_imbalance_pct": monitor.get("inventory_repair_imbalance_pct", 0.0),
        "inventory_repair_trigger_imbalance_pct": monitor.get("inventory_repair_trigger_imbalance_pct", 0.0),
        "cached_avail": monitor.get("cached_avail", round(balance, 2)),
        "cached_frozen": monitor.get("cached_frozen", 0.0),
        "cached_equity": monitor.get("cached_equity", round(balance, 2)),
        "margin_load_pct": monitor.get("margin_load_pct", 0.0),
        "effective_base_pos_usdt": monitor.get("effective_base_pos_usdt", 0.0),
        "session_pnl": round(session_pnl, 2),
        "daily_pnl": round(float(status.get("daily_pnl", 0) or 0), 2),
        "realized_pnl": round(realized_pnl, 2),
        "total_fees": round(total_fees, 4),
        "fee_load_ratio": round(fee_load, 4),
        "fee_load_pct": round(fee_load * 100, 2),
        "halted": bool(status.get("halted", False)),
        "halt_reason": status.get("halt_reason", ""),
        # Deprecated aliases retained for clients during the API migration.
        "rebalance_ready": monitor.get("rebalance_ready", monitor.get("inventory_repair_ready", False)),
        "rebalance_reason": monitor.get("rebalance_reason", monitor.get("inventory_repair_reason", "")),
        "rebalance_cooldown_sec": monitor.get("rebalance_cooldown_sec", 0.0),
        "rebalance_edge_pct": monitor.get("rebalance_edge_pct", 0.0),
        "rebalance_required_edge_pct": monitor.get("rebalance_required_edge_pct", 0.0),
        "rebalance_trigger_imbalance_pct": monitor.get("rebalance_trigger_imbalance_pct", 0.0),
        "global_add_block_reason": monitor.get("global_add_block_reason", ""),
        "global_add_cooldown_sec": monitor.get("global_add_cooldown_sec", 0.0),
        "long_add_trigger_price": monitor.get("long_add_trigger_price", 0.0),
        "short_add_trigger_price": monitor.get("short_add_trigger_price", 0.0),
        "long_add_gap_pct": monitor.get("long_add_gap_pct", 0.0),
        "short_add_gap_pct": monitor.get("short_add_gap_pct", 0.0),
        "long_add_block_reason": monitor.get("long_add_block_reason", ""),
        "short_add_block_reason": monitor.get("short_add_block_reason", ""),
        "long_add_cooldown_sec": monitor.get("long_add_cooldown_sec", 0.0),
        "short_add_cooldown_sec": monitor.get("short_add_cooldown_sec", 0.0),
        "last_event_type": monitor.get("last_event_type", ""),
        "last_event_side": monitor.get("last_event_side", ""),
        "last_event_reason": monitor.get("last_event_reason", ""),
        "last_event_detail": monitor.get("last_event_detail", ""),
        "last_event_price": monitor.get("last_event_price", 0.0),
        "last_event_qty": monitor.get("last_event_qty", 0.0),
        "last_event_at": monitor.get("last_event_at", 0),
        "updated_at": snapshot.get("updated_at", 0),
    }

def _build_strategy_loop_summary(snapshot: dict) -> list:
    rows = []
    strategies = snapshot.get("strategies", {}) if isinstance(snapshot, dict) else {}
    for runtime, state in strategies.items():
        if not isinstance(state, dict):
            continue
        monitor = state.get("monitor", {}) if isinstance(state.get("monitor"), dict) else {}
        tracker = monitor.get("order_tracker", {}) if isinstance(monitor.get("order_tracker"), dict) else {}
        rows.append({
            "runtime": runtime,
            "symbol": state.get("symbol", ""),
            "running": bool(state.get("running")),
            "halted": bool(state.get("halted")),
            "dominant_side": monitor.get("dominant_side", "balanced"),
            "margin_load_pct": monitor.get("margin_load_pct", 0.0),
            "tp_order_count": monitor.get("tp_order_count", state.get("tp_active", 0)),
            "tp_max": state.get("tp_max", 0),
            "grid_count": tracker.get("grid_count", monitor.get("grid_long_count", 0) + monitor.get("grid_short_count", 0)),
            "lost_orders": tracker.get("lost", 0),
            "long_qty": state.get("long_qty", 0.0),
            "short_qty": state.get("short_qty", 0.0),
            "long_block": monitor.get("long_add_block_reason", ""),
            "short_block": monitor.get("short_add_block_reason", ""),
            "grid_limit": monitor.get("grid_layer_limit_reason", ""),
            "opening_guard_total": (monitor.get("opening_guard_stats", {}) or {}).get("total", 0),
            "opening_guard_top_reason": (monitor.get("opening_guard_stats", {}) or {}).get("top_reason", ""),
            "inventory_repair_ready": monitor.get("inventory_repair_ready", monitor.get("inventory_repair_active", False)),
            "inventory_repair_reason": monitor.get("inventory_repair_reason", monitor.get("rebalance_reason", "")),
            "inventory_repair_side": monitor.get("inventory_repair_side", ""),
            "inventory_repair_net_notional": monitor.get("inventory_repair_net_notional", 0.0),
            "rebalance_reason": monitor.get("rebalance_reason", monitor.get("inventory_repair_reason", "")),
            "last_event_type": monitor.get("last_event_type", ""),
            "last_event_reason": monitor.get("last_event_reason", ""),
            "updated_at": snapshot.get("updated_at", 0),
        })
    return rows


def _build_engine_runs(snapshot: dict) -> list:
    """Return one UI-compatible run row per live strategy profile.

    The old dashboard expected a legacy ``runs`` array and otherwise rendered a
    hard-coded CRV card.  Keep the response shape stable while sourcing every
    row from the current multi-profile status snapshot.
    """
    rows = []
    strategies = snapshot.get("strategies", {}) if isinstance(snapshot, dict) else {}
    for runtime, state in strategies.items():
        if not isinstance(state, dict):
            continue
        monitor = state.get("monitor", {}) if isinstance(state.get("monitor"), dict) else {}
        rows.append({
            "runtime": runtime,
            "name": state.get("profile_name") or state.get("symbol") or runtime,
            "symbol": state.get("symbol", ""),
            "profile_slug": state.get("profile_slug", ""),
            "status": "running" if state.get("running") else ("halted" if state.get("halted") else "stopped"),
            "running": bool(state.get("running")),
            "halted": bool(state.get("halted")),
            "halt_reason": state.get("halt_reason", ""),
            "pid": state.get("pid"),
            "pnl": state.get("pnl", 0.0),
            "daily_pnl": state.get("daily_pnl", 0.0),
            "total_fees": state.get("total_fees", 0.0),
            "tp_order_count": monitor.get("tp_order_count", state.get("tp_active", 0)),
            "tp_max": state.get("tp_max", 0),
            "grid_long_count": monitor.get("grid_long_count", 0),
            "grid_short_count": monitor.get("grid_short_count", 0),
            "long_qty": state.get("long_qty", 0.0),
            "short_qty": state.get("short_qty", 0.0),
        })
    return rows


def _read_latest_portfolio_budget_audit() -> dict:
    audit_dir = PROJ / "reports" / "portfolio_budget"
    if not audit_dir.exists():
        return {}
    files = sorted(audit_dir.glob("portfolio_budget_apply_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {}
    data = _load_json_file(files[0])
    data["path"] = str(files[0].relative_to(PROJ))
    return data

# ========== 认证 ==========
class LoginForm(BaseModel):
    email: str = ""
    username: str = ""
    password: str

@router.post("/auth/login")
async def api_login(form: LoginForm):
    from auth import login as auth_login
    login_id = (form.email or form.username or "").strip()
    token = auth_login(login_id, form.password)
    if token:
        return {"code": 0, "token": token, "msg": "登录成功"}
    return {"code": 1, "msg": "邮箱或密码错误"}

@router.post("/auth/logout")
async def api_logout(token: str = ""):
    from auth import revoke_token
    revoke_token(token)
    return {"code": 0, "msg": "已退出"}

@router.post("/auth/change-pw")
async def api_change_pw(data: dict):
    from auth import change_password_by_token
    token = data.get("token", "")
    ok = change_password_by_token(token, data.get("old_pw", ""), data.get("new_pw", ""))
    return {"code": 0 if ok else 1, "msg": "修改成功" if ok else "旧密码错误"}

# ========== API密钥 ==========
@router.get("/config/api-keys")
async def get_api_keys(token: str = ""):
    require_admin_token(token)
    try:
        import sys
        sys.path.insert(0, str(PROJ))
        from api.crypto_utils import CredentialManager
        cfg = load_engine_config()
        cm = CredentialManager(cfg)
        if cm.exists():
            # 只是检查是否有密钥文件，不返回明文
            return {"code": 0, "has_keys": True, "key_file": cfg.get("exchange",{}).get("okx",{}).get("key_file","")}
        return {"code": 0, "has_keys": False}
    except Exception as e:
        return {"code": 0, "has_keys": False, "note": str(e)}

class APIKeyForm(BaseModel):
    api_key: str
    api_secret: str
    passphrase: str
    password: str

class BatchStrategyForm(BaseModel):
    api_key_id: int
    symbols: list[str]
    leverage: int = 3
    capital_budget_usdt: float = 0
    base_position_usdt: float = 5
    max_add_layers: int = 6
    add_step: float = 0.003
    add_multiplier: float = 1.25
    first_tp_ratio: float = 0.0065
    final_tp_ratio: float = 0.013
    max_daily_loss: float = 20
    max_total_loss: float = 50
    max_drawdown: float = 15
    max_margin_ratio: float = 0.5
    name_prefix: str = "client"

@router.post("/config/api-keys")
async def save_api_keys(form: APIKeyForm, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    if not form.api_key or not form.api_secret or not form.passphrase:
        return {"code": 1, "msg": "密钥不完整"}
    try:
        import sys
        sys.path.insert(0, str(PROJ))
        from api.crypto_utils import CredentialManager
        cfg = load_engine_config()
        precheck = _check_api_key_permissions(form.api_key, form.api_secret, form.passphrase)
        if not precheck.get("can_read_balance"):
            return {"code": 1, "msg": precheck.get("msg", "API Key 权限检测失败"), "warnings": precheck.get("warnings", [])}
        cm = CredentialManager(cfg)
        cm.set_password(form.password)
        cm.save(form.api_key, form.api_secret, form.passphrase)
        save_api_password_secure(form.password)
        return {"code": 0, "msg": "密钥已加密保存", "precheck": precheck, "warnings": precheck.get("warnings", [])}
    except Exception as e:
        return {"code": 1, "msg": f"保存失败: {e}"}

@router.post("/config/test-connection")
async def test_connection(data: dict, token: str = ""):
    require_admin_token(token)
    ok, msg = test_api_connection(
        data.get("api_key",""), data.get("api_secret",""), data.get("passphrase","")
    )
    return {"code": 0 if ok else 1, "msg": "连接成功" if ok else f"连接失败: {msg}"}

# ========== 运行模式 ==========
@router.get("/config/mode")
async def get_mode(token: str = ""):
    require_admin_token(token)
    cfg = load_engine_config()
    return {"code": 0, "mode": "demo" if cfg.get("global",{}).get("demo",False) else "live"}

class ModeForm(BaseModel):
    mode: str

@router.post("/config/mode")
async def set_mode(form: ModeForm, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    cfg = load_engine_config()
    before = {"global": dict(cfg.get("global", {}))}
    if form.mode == "demo":
        cfg["global"]["demo"] = True
    else:
        cfg["global"]["demo"] = False
    save_engine_config(cfg)
    revision = _append_config_history("mode", before, {"global": dict(cfg.get("global", {}))})
    return {"code": 0, "msg": f"已切换至{'演示' if form.mode=='demo' else '实盘'}模式", "revision": revision}

@router.get("/config/full")
async def get_full_config(token: str = ""):
    require_admin_token(token)
    return {"code": 0, "config": load_engine_config()}

@router.get("/config/history")
async def get_config_history(limit: int = 20, token: str = ""):
    require_admin_token(token)
    return {"code": 0, "items": _read_config_history(limit)}

@router.post("/engine/config")
async def save_engine_config_merged(data: dict, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    current = load_engine_config()
    merged = _merge_config_sections(current, data)
    save_engine_config(merged)
    revision = _append_config_history("engine", current, merged)
    return {"code": 0, "msg": "配置已保存", "revision": revision}

# ========== 现货策略配置 ==========
@router.get("/config/spot")
async def get_spot_config(token: str = ""):
    require_admin_token(token)
    cfg = load_engine_config()
    spot = cfg.get("spot", {})
    return {"code": 0, "config": spot}

@router.post("/config/spot")
async def save_spot_config(data: dict, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    cfg = load_engine_config()
    spot = cfg.get("spot", {})
    before = dict(spot)
    for k, v in data.get("config", {}).items():
        spot[k] = v
    cfg["spot"] = spot
    save_engine_config(cfg)
    revision = _append_config_history("spot", before, dict(spot))
    return {"code": 0, "msg": "现货配置已保存", "revision": revision}

# ========== 合约策略配置 ==========
@router.get("/config/futures")
async def get_futures_config(token: str = ""):
    require_admin_token(token)
    cfg = load_engine_config()
    fut = cfg.get("futures", {})
    return {"code": 0, "config": fut}

@router.post("/config/futures")
async def save_futures_config(data: dict, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    cfg = load_engine_config()
    fut = cfg.get("futures", {})
    before = dict(fut)
    for k, v in data.get("config", {}).items():
        fut[k] = v
    cfg["futures"] = fut
    save_engine_config(cfg)
    revision = _append_config_history("futures", before, dict(fut))
    return {"code": 0, "msg": "合约配置已保存", "revision": revision}

# ========== 风控参数 ==========
@router.get("/config/risk")
async def get_risk_config(token: str = ""):
    require_admin_token(token)
    cfg = load_engine_config()
    risk = cfg.get("risk", {})
    # 增加全局风控
    global_cfg = cfg.get("global", {})
    risk["_global"] = {
        "max_daily_loss": global_cfg.get("max_daily_loss", 1000),
        "max_position_per_strategy": global_cfg.get("max_position_per_strategy", 10),
        "max_loss_per_trade": global_cfg.get("max_loss_per_trade", 100),
        "emergency_stop": global_cfg.get("emergency_stop", True),
    }
    return {"code": 0, "config": risk}

@router.post("/config/risk")
async def save_risk_config(data: dict, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    cfg = load_engine_config()
    risk = cfg.get("risk", {})
    before = {
        "risk": json.loads(json.dumps(risk)),
        "global": dict(cfg.get("global", {})),
    }
    for section in ["black_swan", "liquidation"]:
        if section in data.get("config", {}):
            risk[section] = {**risk.get(section, {}), **data["config"][section]}
    cfg["risk"] = risk
    # 全局风控
    gl = data.get("config", {}).get("_global", {})
    if "global" not in cfg:
        cfg["global"] = {}
    for k in ["max_daily_loss", "max_position_per_strategy", "max_loss_per_trade", "emergency_stop"]:
        if k in gl:
            cfg["global"][k] = gl[k]
    save_engine_config(cfg)
    after = {
        "risk": json.loads(json.dumps(cfg.get("risk", {}))),
        "global": dict(cfg.get("global", {})),
    }
    revision = _append_config_history("risk", before, after)
    return {"code": 0, "msg": "风控配置已保存", "revision": revision}

# ========== 进程控制 ==========
def _run_script(name):
    script = PROJ / "bin" / name
    if not script.exists():
        return {"code": 1, "msg": f"脚本不存在: {name}"}
    try:
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30,
            cwd=str(PROJ)
        )
        return {"code": 0, "msg": result.stdout[:500], "err": result.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"code": 0, "msg": "命令已超时但可能已执行"}
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@router.post("/process/stop")
async def process_stop(token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    return _run_script("stop.sh")

@router.post("/process/restart")
async def process_restart(token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    return _run_script("restart.sh")

@router.post("/process/clear-logs")
async def process_clear_logs(token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    log_dir = PROJ / "logs"
    for f in log_dir.glob("*.log*"):
        try:
            f.unlink()
        except:
            pass
    return {"code": 0, "msg": "日志已清空"}

@router.post("/process/reload")
async def process_reload(token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    # 发送SIGHUP信号到主进程
    pid_file = PROJ / "data" / "run.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 1)  # SIGHUP
            return {"code": 0, "msg": "已发送重载信号"}
        except:
            return {"code": 1, "msg": "发送信号失败"}
    return {"code": 1, "msg": "未找到运行中的进程"}

# ========== 状态信息 ==========
@router.get("/status")
async def get_status(token: str = ""):
    require_admin_token(token)
    snapshot = _load_strategy_snapshot()
    pid_file = PROJ / "data" / "run.pid"
    running = False
    pid = 0
    uptime = 0
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            running = True
            import psutil
            p = psutil.Process(pid)
            uptime = int(time.time() - p.create_time())
        except:
            running = False
    # 读取盈亏记录
    pnl = {"total_trades": 0, "total_pnl": 0, "win_rate": 0}
    pnl_file = PROJ / "data" / "pnl_records.csv"
    if pnl_file.exists():
        try:
            import csv
            total, wins, pnl_sum = 0, 0, 0.0
            with open(pnl_file, "r") as f:
                for row in csv.DictReader(f):
                    total += 1
                    p = float(row.get("pnl", 0))
                    pnl_sum += p
                    if p > 0: wins += 1
            pnl = {"total_trades": total, "total_pnl": round(pnl_sum, 2), "win_rate": round(wins/total*100,1) if total else 0}
        except:
            pass
    cfg = load_engine_config()
    # 查余额
    balance = 0.0
    risk = _build_risk_snapshot(balance)
    monitor = _build_monitor_snapshot(snapshot, balance)

    return {
        "code": 0,
        "running": running,
        "pid": pid,
        "uptime": uptime,
        "mode": "demo" if cfg.get("global",{}).get("demo",False) else "live",
        "pnl": pnl,
        "balance": round(balance, 2),
        "risk": risk,
        "monitor": monitor,
        "snapshot": snapshot,
    }

# ========== 日志查看 ==========
@router.get("/logs")
async def get_logs(lines: int = 200, level: str = "", token: str = ""):
    require_admin_token(token)
    log_file = PROJ / "logs" / "trading.log"
    if not log_file.exists():
        return {"code": 0, "lines": []}
    try:
        with open(log_file, "r", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-min(lines, len(all_lines)):]
        if level:
            tail = [l for l in tail if f"[{level}]" in l or f"[{level.upper()}]" in l]
        return {"code": 0, "lines": [l.rstrip("\n\r") for l in tail]}
    except Exception as e:
        return {"code": 1, "msg": str(e)}

# ========== 系统监控 ==========

# ========== 数据分析 ==========
@router.get("/analytics/trades")
async def get_analytics_trades(limit: int = 30, token: str = ""):
    require_admin_token(token)
    try:
        import sys; sys.path.insert(0, str(PROJ))
        from utils.database import Database
        db = Database(str(PROJ / "data" / "trading.db"))
        trades = db.get_recent_trades(limit)
        summary = db.get_trade_summary()
    except:
        trades, summary = [], {}
    return {"code": 0, "trades": trades, "summary": summary}

@router.get("/analytics/equity")
async def get_analytics_equity(token: str = ""):
    require_admin_token(token)
    try:
        import sys; sys.path.insert(0, str(PROJ))
        from utils.database import Database
        db = Database(str(PROJ / "data" / "trading.db"))
        curve = db.get_equity_curve()
    except:
        curve = []
    return {"code": 0, "curve": curve}

@router.get("/analytics/execution")
async def get_analytics_execution(limit: int = 500, token: str = ""):
    require_admin_token(token)
    return {"code": 0, "metrics": _build_execution_metrics(limit)}

@router.get("/ops/health")
async def get_ops_health(token: str = ""):
    require_admin_token(token)
    return {"code": 0, "health": _build_ops_health()}

@router.get("/monitor")
async def get_monitor(token: str = ""):
    require_admin_token(token)
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "code": 0,
            "cpu": cpu,
            "memory": {"total": mem.total, "used": mem.used, "percent": mem.percent},
            "disk": {"total": disk.total, "used": disk.used, "percent": disk.percent},
        }
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@router.get("/observability/market")
async def get_market_observability(token: str = ""):
    require_admin_token(token)
    return _build_market_observability()


@router.get("/strategies")
async def get_strategies(token: str = ""):
    require_admin_token(token)
    try:
        sys.path.insert(0, str(PROJ))
        from utils.database import Database
        db = Database(str(PROJ / "data" / "trading.db"))
        runs = db.get_active_runs()
        pid_file = PROJ / "data" / "run.pid"
        pid = None
        if pid_file.exists():
            try: pid = int(pid_file.read_text().strip())
            except: pass
        running = False
        if pid:
            try: os.kill(pid, 0); running = True
            except: pass
        return {"code": 0, "runs": runs, "pid": pid, "running": running}
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@router.get("/strategies/engine-status")
async def get_engine_status(token: str = ""):
    require_admin_token(token)
    pid_file = PROJ / "data" / "run.pid"
    pid = None; running = False
    if pid_file.exists():
        try: pid = int(pid_file.read_text().strip())
        except: pass
    if pid:
        try: os.kill(pid, 0); running = True
        except: pass
    strategy_data = {}
    try:
        from utils.database import Database as DB
        d = DB(str(PROJ / "data" / "trading.db"))
        summary = d.get_trade_summary()
        equity_curve = d.get_equity_curve()
        recent = d.get_recent_trades(5)
        strategy_data = {"summary": summary, "equity_count": len(equity_curve), "equity_curve": equity_curve, "recent_trades": recent}
    except: pass
    pnl_data = {}
    real_upnl = 0.0
    real_equity = 0.0
    pnl_file = PROJ / "data" / "fee_pnl.json"
    if pnl_file.exists():
        try: import json; pnl_data = json.loads(pnl_file.read_text())
        except: pass
    # Get real account PnL from exchange
    primary = _primary_strategy_snapshot()
    real_equity = _safe_float_value(primary.get("cached_equity", 0))
    init_eq = _safe_float_value(pnl_data.get("initial_equity", 0))
    real_upnl = real_equity - init_eq if init_eq > 0 else 0
    pnl_data["real_upnl"] = round(real_upnl, 2)
    pnl_data["real_equity"] = round(real_equity, 2)
    risk = _build_risk_snapshot(real_equity)
    snapshot = _load_strategy_snapshot()
    monitor = _build_monitor_snapshot(snapshot, real_equity)
    price = None
    log_file = PROJ / "logs" / "trading.log"
    if log_file.exists():
        try:
            last_lines = log_file.read_text().strip().split(chr(10))
            for line in reversed(last_lines):
                if "skip @" in line or "->" in line:
                    import re
                    m = re.search(r'@\s*([\d.]+)', line)
                    if m: price = float(m.group(1)); break
        except: pass
    return {"code": 0, "pid": pid, "running": running, "strategy": strategy_data, "pnl": pnl_data, "risk": risk, "current_price": price, "monitor": monitor, "snapshot": snapshot, "runs": _build_engine_runs(snapshot), "loop_summary": _build_strategy_loop_summary(snapshot), "portfolio_budget_audit": _read_latest_portfolio_budget_audit()}

@router.post("/process/reset-accounting")
async def process_reset_accounting(token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    backup_dir = PROJ / "data" / "backups" / f"reset_{stamp}_accounting"
    backup_dir.mkdir(parents=True, exist_ok=True)
    data_dir = PROJ / "data"
    logs_dir = PROJ / "logs"
    pnl_file = data_dir / "pnl_records.csv"
    fee_file = data_dir / "fee_pnl.json"
    db_path = data_dir / "trading.db"
    running_before = _current_engine_pid() > 0
    stop_result = {"code": 0, "msg": "engine already stopped"}
    if running_before:
        stop_result = _run_script("stop.sh")
        time.sleep(2)

    backup_map = [
        (pnl_file, "pnl_records_old.csv"),
        (fee_file, "fee_pnl.json"),
        (data_dir / "halt_state.json", "halt_state.json"),
        (data_dir / "strategies_status.json", "strategies_status.json"),
        (db_path, "trading.db"),
        (data_dir / "trading.db-shm", "trading.db-shm"),
        (data_dir / "trading.db-wal", "trading.db-wal"),
        (logs_dir / "trading.log", "trading.log"),
        (logs_dir / "web.log", "web.log"),
    ]
    backed_up = []
    for src, name in backup_map:
        if src.exists():
            shutil.copy2(src, backup_dir / name)
            backed_up.append(name)

    existing = _load_json_file(fee_file)
    baseline_equity = float(existing.get("risk_equity_baseline", existing.get("initial_equity", 0)) or 0)
    latest_fill_ts_ms = int(_safe_float_value(existing.get("last_fill_sync_ts_ms", 0)))
    recent_fill_ids = [str(x) for x in (existing.get("recent_fill_ids", []) or []) if x][-200:]
    try:
        client, cfg = _load_engine_okx_client()
        baseline_equity = client.get_total_equity() or baseline_equity
        symbol = cfg.get("futures", {}).get("symbol", "")
        if symbol:
            resp = client._call(client._trade.get_fills, instType="SWAP", instId=symbol, limit="100")
            fills = resp.get("data", []) or []
            if fills:
                fills = sorted(fills, key=lambda fill: int(_safe_float_value(fill.get("ts", 0))))
                current_fill_id = recent_fill_ids[-1] if recent_fill_ids else ""
                if current_fill_id:
                    idx = next((i for i, fill in enumerate(fills) if _okx_fill_id(fill) == current_fill_id), -1)
                    if idx >= 0:
                        fills = fills[: idx + 1]
                if fills:
                    recent_fill_ids = [_okx_fill_id(fill) for fill in fills][-200:]
                    latest_fill_ts_ms = max(int(_safe_float_value(fill.get("ts", 0))) for fill in fills)
    except Exception:
        pass

    header = "time,symbol,side,qty,entry,exit,gross,fee,net\n"
    os.makedirs(data_dir, exist_ok=True)
    pnl_file.write_text(header, encoding="utf-8")

    payload = {
        "total_realized_pnl": 0.0,
        "total_fees_paid": 0.0,
        "total_funding_paid": 0.0,
        "initial_equity": round(baseline_equity, 2),
        "risk_equity_baseline": round(baseline_equity, 2),
        "session_start_total_pnl": 0.0,
        "daily_pnl_date": now.strftime("%Y-%m-%d"),
        "day_start_total_pnl": 0.0,
        "day_equity_baseline": round(baseline_equity, 2),
        "daily_stop_until": 0.0,
        "daily_stop_reason": "",
        "last_update": now.strftime("%Y-%m-%d %H:%M:%S"),
        "last_fill_sync_ts_ms": int(latest_fill_ts_ms),
        "recent_fill_ids": recent_fill_ids[-200:],
    }
    fee_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    db_reset = {"trades_deleted": 0, "equity_deleted": 0}
    if db_path.exists():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades")
            db_reset["trades_deleted"] = int(cur.fetchone()[0] or 0)
            cur.execute("DELETE FROM trades")
            cur.execute("SELECT COUNT(*) FROM equity")
            db_reset["equity_deleted"] = int(cur.fetchone()[0] or 0)
            cur.execute("DELETE FROM equity")
            conn.commit()
        finally:
            conn.close()

    restart_result = {"code": 0, "msg": "engine remained stopped"}
    if running_before:
        restart_result = _run_script("restart.sh")

    return {
        "code": 0,
        "msg": "accounting reset complete",
        "backup_dir": str(backup_dir.relative_to(PROJ)),
        "running_before": running_before,
        "stop_result": stop_result,
        "restart_result": restart_result,
        "backed_up": backed_up,
        "baseline_equity": round(baseline_equity, 2),
        "fill_anchor_ts_ms": int(latest_fill_ts_ms),
        "recent_fill_ids": len(recent_fill_ids),
        "db_reset": db_reset,
    }




def _customer_strategy_label(symbol: str, profile_name: str = "") -> dict:
    symbol = (symbol or "").strip().upper()
    base = symbol.split("-", 1)[0]
    labels = {
        "CRV-USDT-SWAP": ("稳健主策略 CRV", "系统当前主策略，建议先用小资金完成实盘验证"),
        "FARTCOIN-USDT-SWAP": ("高波动实验 FARTCOIN", "波动较大，仅建议小资金实盘验证"),
        "BTC-USDT-SWAP": ("主流币策略 BTC", "主流币策略，系统自动使用币种参数"),
        "ETH-USDT-SWAP": ("主流币策略 ETH", "主流币策略，系统自动使用币种参数"),
        "SOL-USDT-SWAP": ("波动币策略 SOL", "波动相对较高，建议控制资金规模"),
        "BNB-USDT-SWAP": ("主流币策略 BNB", "主流币策略，系统自动使用币种参数"),
        "XRP-USDT-SWAP": ("波动币策略 XRP", "波动相对较高，建议控制资金规模"),
        "DOGE-USDT-SWAP": ("波动币策略 DOGE", "流动性稳定，先做模拟和小资金验证"),
        "DRAM-USDT-SWAP": ("高波动观察 DRAM", "波动和趋势风险较高，仅建议小资金实盘验证"),
        "ETHFI-USDT-SWAP": ("高波动策略 ETHFI", "波动和成交较活跃，建议控制资金规模"),
    }
    title, description = labels.get(symbol, (profile_name or f"推荐策略 {base}", "系统推荐实盘策略，参数由引擎自动配置"))
    risk = "实验" if "实验" in title or symbol.startswith("FARTCOIN") or base in {"DRAM"} else ("波动" if "波动" in title or base in {"SOL", "XRP"} else "稳健")
    return {"title": title, "description": description, "risk_tag": risk}

def _customer_recommended_profiles() -> list:
    customer_symbols = {
        "CRV-USDT-SWAP",
        "FARTCOIN-USDT-SWAP",
        "DRAM-USDT-SWAP",
        "BTC-USDT-SWAP",
        "BNB-USDT-SWAP",
        "ETH-USDT-SWAP",
        "SOL-USDT-SWAP",
        "XRP-USDT-SWAP",
        "DOGE-USDT-SWAP",
        "ETHFI-USDT-SWAP",
    }
    items = []
    seen = set()

    def add_profile(profile: dict, source: str):
        symbol = (profile.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen or symbol not in customer_symbols:
            return
        seen.add(symbol)
        cfg = profile.get("config") or {}
        label = _customer_strategy_label(symbol, profile.get("name") or "")
        items.append({
            "slug": profile.get("slug") or symbol.lower().replace("-", "_"),
            "name": label["title"],
            "symbol": symbol,
            "description": label["description"],
            "risk_tag": label["risk_tag"],
            "base_position_usdt": float(cfg.get("base_position_usdt") or 5),
            "leverage": int(cfg.get("leverage") or 3),
            "max_add_layers": int(cfg.get("max_add_layers") or 6),
            "add_step": float(cfg.get("add_step") or 0.003),
            "add_multiplier": float(cfg.get("add_multiplier") or 1.25),
            "first_tp_ratio": float(cfg.get("first_tp_ratio") or 0.0065),
            "final_tp_ratio": float(cfg.get("final_tp_ratio") or 0.013),
            "max_margin_ratio": float(cfg.get("max_margin_ratio") or 0.5),
            "enabled": bool(profile.get("enabled")),
            "running": bool(profile.get("running")),
            "source": source,
        })

    for profile in _list_engine_profiles().get("profiles", []):
        add_profile(profile, "enabled")

    standby_dir = PROJ / "config" / "standby"
    for path in sorted(standby_dir.glob("config_*.yaml")):
        cfg = _load_yaml_file(path)
        fut = cfg.get("futures", {}) if isinstance(cfg.get("futures", {}), dict) else {}
        profile_meta = cfg.get("profile", {}) if isinstance(cfg.get("profile", {}), dict) else {}
        symbol = fut.get("symbol", "")
        slug = path.stem.replace("config_", "")
        add_profile({
            "slug": slug,
            "name": profile_meta.get("name") or symbol or slug,
            "symbol": symbol,
            "config": {
                "symbol": symbol,
                "leverage": fut.get("leverage", 0),
                "base_position_usdt": fut.get("base_position_usdt", 0),
                "max_add_layers": fut.get("max_add_layers", 0),
                "add_step": fut.get("add_step", 0),
                "add_multiplier": fut.get("add_multiplier", 0),
                "first_tp_ratio": fut.get("first_tp_ratio", 0),
                "final_tp_ratio": fut.get("final_tp_ratio", 0),
                "max_margin_ratio": fut.get("max_margin_ratio", 0),
            },
            "enabled": False,
            "running": False,
        }, "standby")
    return items


def _customer_profile_for_symbol(symbol: str) -> dict:
    symbol = (symbol or "").strip().upper()
    for item in _customer_recommended_profiles():
        if item.get("symbol") == symbol:
            return item
    return {}


def _apply_customer_profile_defaults(data: dict) -> dict:
    profile = _customer_profile_for_symbol(data.get("symbol", ""))
    if not profile:
        return {}
    merged = dict(data)
    merged["symbol"] = profile["symbol"]
    merged["name"] = profile["name"]
    for key in ["base_position_usdt", "leverage", "max_add_layers", "add_step", "add_multiplier", "first_tp_ratio", "final_tp_ratio", "max_margin_ratio"]:
        merged[key] = profile[key]
    merged["paper_mode"] = False
    return merged

@router.post("/auth/register")
async def api_register(data: dict):
    from auth import register
    email = data.get("email", "").strip().lower()
    password = data.get("password", "").strip()
    if not email or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return {"code": 1, "msg": "请输入有效邮箱"}
    if not password or len(password) < 8:
        return {"code": 1, "msg": "密码至少 8 位"}
    r = register(email, password, email, data.get("invite_code", "").strip())
    if r["ok"]:
        return {"code": 0, "msg": "registered"}
    return {"code": 1, "msg": r["msg"]}


@router.get("/admin/users")
async def api_admin_users(token: str = ""):
    from auth import check_token, is_admin_token
    if not check_token(token) or not is_admin_token(token):
        raise HTTPException(403, "admin required")
    db = Database(str(PROJ / "data" / "trading.db"))
    users = db.list_users_admin()
    db.close()
    return {"code": 0, "users": users}

@router.post("/admin/users/{uid}/disabled")
async def api_admin_set_user_disabled(uid: int, data: dict, token: str = ""):
    from auth import check_token, is_admin_token
    if not check_token(token) or not is_admin_token(token):
        raise HTTPException(403, "admin required")
    db = Database(str(PROJ / "data" / "trading.db"))
    result = db.set_user_disabled(uid, bool(data.get("disabled", True)))
    db.close()
    if result.get("ok"):
        return {"code": 0, "msg": "updated"}
    return {"code": 1, "msg": result.get("msg", "update failed")}

@router.get("/user/info")
async def api_user_info(token: str = ""):
    from auth import check_token, get_user_id, get_username
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    username = get_username(token)
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    info = db.get_user(username)
    db.close()
    return {"code": 0, "user_id": uid, "username": username, "user": info}

@router.get("/user/keys")
async def api_get_keys(token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    keys = db.get_user_api_keys(uid)
    db.close()
    return {"code": 0, "keys": keys}

@router.post("/user/keys")
async def api_save_key(data: dict, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    precheck = _check_api_key_permissions(data.get("api_key", ""), data.get("api_secret", ""), data.get("passphrase", ""))
    if not precheck.get("can_read_balance"):
        db.close()
        return {"code": 1, "msg": precheck.get("msg", "API Key 权限检测失败"), "warnings": precheck.get("warnings", [])}
    r = db.save_api_key(uid, data["api_key"], data["api_secret"], data["passphrase"], data.get("alias", ""))
    db.close()
    if r["ok"]:
        return {"code": 0, "key_id": r["key_id"], "precheck": precheck, "warnings": precheck.get("warnings", [])}
    return {"code": 1, "msg": r["msg"]}

@router.get("/user/strategies")
async def api_get_strategies(token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    from core.process_manager import ProcessManager
    db = Database(str(PROJ / "data" / "trading.db"))
    pm = ProcessManager()
    strategies = pm.list_user_strategies(uid, db)
    db.close()
    return {"code": 0, "strategies": strategies}


@router.get("/user/strategies/{sid}/metrics")
async def api_strategy_metrics(sid: int, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = int(get_user_id(token))
    from utils.database import Database
    from core.process_manager import ProcessManager
    db = Database(str(PROJ / "data" / "trading.db"))
    strategies = db.get_user_strategies(uid)
    strategy = next((item for item in strategies if int(item.get("id")) == sid), None)
    if strategy is None:
        db.close()
        return {"code": 1, "msg": "strategy not found"}
    runtime = ProcessManager().status(sid, db)
    db.close()
    from web.customer_metrics import build_customer_metrics
    strategy_dir = PROJ / "data" / "users" / f"u{uid}_s{sid}"
    metrics = build_customer_metrics(strategy_dir, str(strategy.get("symbol") or ""))
    return {
        "code": 0,
        "strategy_id": sid,
        "symbol": strategy.get("symbol"),
        "status": strategy.get("status"),
        "runtime": runtime,
        "metrics": metrics,
    }


@router.post("/user/keys/precheck")
async def api_precheck_user_key(data: dict, token: str = ""):
    from auth import check_token
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    return {"code": 0, "precheck": _check_api_key_permissions(data.get("api_key", ""), data.get("api_secret", ""), data.get("passphrase", ""))}

@router.post("/strategy/symbol-admission")
async def api_symbol_admission(data: dict, token: str = ""):
    from auth import check_token
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    return {"code": 0, "result": _symbol_admission_payload(data.get("symbol", ""), data.get("initial_margin_usdt", data.get("base_position_usdt", 0)), data.get("leverage", 3))}


@router.get("/user/strategy-recommendations")
async def api_strategy_recommendations(token: str = ""):
    from auth import check_token
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    return {"code": 0, "recommendations": _customer_recommended_profiles()}

@router.post("/user/strategies/precheck")
async def api_strategy_precheck(data: dict, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    data = _apply_customer_profile_defaults(data)
    if not data:
        return {"code": 1, "msg": "请选择系统推荐策略", "precheck": {"ok": False, "paper_mode": False, "warnings": ["请选择系统内置推荐交易对"]}}
    if not data.get("api_key_id"):
        return {"code": 1, "msg": "请先选择 API Key", "precheck": {"ok": False, "paper_mode": False, "warnings": ["请先保存并选择自己的 API Key"]}}
    key_data = None
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    keys = db.get_user_api_keys(get_user_id(token))
    owned_key_ids = {int(k["id"]) for k in keys}
    requested_key_id = int(data.get("api_key_id"))
    if requested_key_id not in owned_key_ids:
        db.close()
        return {"code": 1, "msg": "API Key 不属于当前用户", "precheck": {"ok": False, "paper_mode": False, "warnings": ["请先保存并选择自己的 API Key"]}}
    key_data = db.get_user_api_key_data(requested_key_id)
    db.close()
    return {"code": 0, "precheck": _strategy_precheck_payload(data, key_data)}

@router.post("/user/strategies")
async def api_create_strategy(data: dict, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    user_key_ids = {int(k["id"]) for k in db.get_user_api_keys(uid)}
    api_key_id = int(data.get("api_key_id", 0) or 0)
    if api_key_id not in user_key_ids:
        db.close()
        return {"code": 1, "msg": "API Key 不属于当前用户"}
    data = _apply_customer_profile_defaults(data)
    if not data:
        db.close()
        return {"code": 1, "msg": "请选择系统推荐策略"}
    key_data = db.get_user_api_key_data(api_key_id)
    precheck = _strategy_precheck_payload(data, key_data)
    if not precheck.get("ok"):
        db.close()
        return {"code": 1, "msg": "策略预检失败", "precheck": precheck}
    data["paper_mode"] = False
    r = db.create_strategy(uid, data["api_key_id"], data["symbol"],
                          name=data.get("name", ""),
                          leverage=int(data.get("leverage", 3)),
                          base_position_usdt=float(data.get("base_position_usdt", 5)),
                          max_add_layers=int(data.get("max_add_layers", 6)),
                          add_step=float(data.get("add_step", 0.003)),
                          add_multiplier=float(data.get("add_multiplier", 1.25)),
                          first_tp_ratio=float(data.get("first_tp_ratio", 0.0065)),
                          final_tp_ratio=float(data.get("final_tp_ratio", 0.013)),
                          max_margin_ratio=float(data.get("max_margin_ratio", 0.5)),
                          capital_budget_usdt=float(data.get("capital_budget_usdt", data.get("total_capital_usdt", 0)) or 0),
                          max_daily_loss=float(data.get("max_daily_loss", 20)),
                          max_total_loss=float(data.get("max_total_loss", 50)),
                          max_drawdown=float(data.get("max_drawdown", 15)))
    db.close()
    if r["ok"]:
        return {"code": 0, "strategy_id": r["strategy_id"], "paper_mode": False, "precheck": precheck, "warnings": precheck.get("warnings", [])}
    return {"code": 1, "msg": r["msg"]}

@router.post("/user/strategies/batch")
async def api_create_strategies_batch(form: BatchStrategyForm, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    symbols = []
    seen = set()
    for symbol in form.symbols or []:
        value = str(symbol or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        symbols.append(value)
    if not symbols:
        return {"code": 1, "msg": "请选择至少一个币种"}
    if len(symbols) > 5:
        return {"code": 1, "msg": "最多支持 5 个币种"}
    db = Database(str(PROJ / "data" / "trading.db"))
    keys = db.get_user_api_keys(uid)
    key_ids = {int(k["id"]) for k in keys}
    if form.api_key_id not in key_ids:
        db.close()
        return {"code": 1, "msg": "API Key 不属于当前用户"}
    key_data = db.get_user_api_key_data(int(form.api_key_id))
    results = []
    for idx, symbol in enumerate(symbols, start=1):
        strategy_data = _apply_customer_profile_defaults({
            "symbol": symbol,
            "api_key_id": form.api_key_id,
            "leverage": form.leverage,
            "capital_budget_usdt": form.capital_budget_usdt,
            "base_position_usdt": form.base_position_usdt,
            "max_add_layers": form.max_add_layers,
            "add_step": form.add_step,
            "first_tp_ratio": form.first_tp_ratio,
        })
        if not strategy_data:
            results.append({"symbol": symbol, "ok": False, "msg": "strategy is not open to customers"})
            continue
        precheck = _strategy_precheck_payload(strategy_data, key_data)
        if not precheck.get("ok"):
            results.append({"symbol": symbol, "ok": False, "msg": "策略预检失败", "precheck": precheck})
            continue
        symbol = strategy_data["symbol"]
        name = f"{form.name_prefix}_{idx}_{symbol.split('-')[0].lower()}"
        r = db.create_strategy(
            uid,
            form.api_key_id,
            symbol,
            name=name,
            leverage=int(strategy_data["leverage"]),
            capital_budget_usdt=float(strategy_data.get("capital_budget_usdt", 0) or 0),
            base_position_usdt=float(strategy_data["base_position_usdt"]),
            max_add_layers=int(strategy_data["max_add_layers"]),
            add_step=float(strategy_data["add_step"]),
            add_multiplier=float(strategy_data["add_multiplier"]),
            first_tp_ratio=float(strategy_data["first_tp_ratio"]),
            final_tp_ratio=float(strategy_data["final_tp_ratio"]),
            max_daily_loss=float(form.max_daily_loss),
            max_total_loss=float(form.max_total_loss),
            max_drawdown=float(form.max_drawdown),
            max_margin_ratio=float(strategy_data["max_margin_ratio"]),
        )
        results.append({"symbol": symbol, "ok": bool(r.get("ok")), "strategy_id": r.get("strategy_id"), "msg": r.get("msg", ""), "precheck": precheck})
    db.close()
    created = sum(1 for item in results if item.get("ok"))
    return {"code": 0 if created else 1, "created_count": created, "paper_mode": False, "warnings": ["批量创建的客户策略为实盘模式，请确认风险后启动"], "items": results}



@router.post("/user/strategies/{sid}/start")
async def api_start_strategy(sid: int, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    from core.process_manager import ProcessManager
    db = Database(str(PROJ / "data" / "trading.db"))
    # Get user strategy
    strategies = db.get_user_strategies(uid)
    strategy = None
    for s in strategies:
        if s["id"] == sid:
            strategy = s
            break
    if not strategy:
        db.close()
        return {"code": 1, "msg": "strategy not found"}
    # Get user API keys
    keys = db.get_user_api_keys(uid)
    api_key_obj = None
    for k in keys:
        if k["id"] == strategy.get("api_key_id"):
            api_key_obj = k
            break
    if not api_key_obj:
        db.close()
        return {"code": 1, "msg": "API key not found, save API key first"}
    # Get decrypted API key data
    key_data = db.get_user_api_key_data(api_key_obj["id"])
    db.close()
    if not key_data.get("ok"):
        return {"code": 1, "msg": key_data.get("msg", "API key data not found")}
    strategy_config = _apply_customer_profile_defaults(strategy)
    precheck = _strategy_precheck_payload(strategy_config, key_data)
    if not precheck.get("ok"):
        return {"code": 1, "msg": "策略启动预检失败", "precheck": precheck}
    pm = ProcessManager()
    db3 = Database(str(PROJ / "data" / "trading.db"))
    result = pm.start(sid, uid, db3, key_data["api_key"], key_data["api_secret"], key_data["passphrase"], strategy)
    db3.close()
    if result.get("ok"):
        return {"code": 0, "pid": result["pid"], "msg": f"started (PID {result['pid']})"}
    return {"code": 1, "msg": result.get("msg", "start failed")}

@router.post("/user/strategies/{sid}/stop")
async def api_stop_strategy(sid: int, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    from core.process_manager import ProcessManager
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    if not any(s.get("id") == sid for s in db.get_user_strategies(uid)):
        db.close()
        return {"code": 1, "msg": "strategy not found"}
    pm = ProcessManager()
    result = pm.stop(sid, db)
    if not result.get("ok"):
        db.update_strategy_status(sid, "stopped")
    db.close()
    if result.get("ok"):
        return {"code": 0, "msg": result.get("msg", "stopped")}
    return {"code": 1, "msg": result.get("msg", "not running")}

@router.get("/user/strategies/{sid}/status")
async def api_strategy_status(sid: int, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token):
        raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    from core.process_manager import ProcessManager
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    if not any(s.get("id") == sid for s in db.get_user_strategies(uid)):
        db.close()
        return {"code": 1, "msg": "strategy not found"}
    pm = ProcessManager()
    status = pm.status(sid, db)
    db.close()
    return {"code": 0, "status": status}




@router.get("/engine/strategies/status")
async def api_engine_strategies(token: str = ""):
    require_admin_token(token)
    import json, os
    sp = "data/strategies_status.json"
    if os.path.exists(sp):
        try:
            st = json.load(open(sp))
            if isinstance(st, dict) and "strategies" in st:
                return {"code": 0, "strategies": st.get("strategies", {}), "engine": {k: v for k, v in st.items() if k != "strategies"}}
            return {"code": 0, "strategies": st}
        except:
            return {"code": 0, "strategies": {}}
    return {"code": 0, "strategies": {}}

@router.post("/engine/strategies/{name}/start")
async def api_engine_start_strategy(name: str, token: str = ""):
    require_admin_token(token)
    command_id = _enqueue_control_command("strategy_start", target=name)
    return {"code": 0, "command_id": command_id, "msg": f"start command queued for {name}"}

@router.post("/engine/strategies/{name}/stop")
async def api_engine_stop_strategy(name: str, token: str = ""):
    require_admin_token(token)
    command_id = _enqueue_control_command("strategy_stop", target=name)
    return {"code": 0, "command_id": command_id, "msg": f"stop command queued for {name}"}


@router.get("/engine/risk-state")
async def api_engine_risk_state(token: str = ""):
    require_admin_token(token)
    db_path = PROJ / "data" / "trading.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT state,version,reason,actor,updated_ts FROM risk_state WHERE id=1"
        ).fetchone()
    if not row:
        return {"code": 1, "msg": "risk state unavailable"}
    return {
        "code": 0,
        "risk_state": {
            "state": row[0],
            "version": row[1],
            "reason": row[2],
            "actor": row[3],
            "updated_ts": row[4],
        },
    }


@router.post("/engine/risk-state")
async def api_set_engine_risk_state(data: dict, token: str = ""):
    require_admin_token(token)
    state = str(data.get("state") or "").upper()
    if state not in {"ACTIVE", "INVENTORY_REPAIR", "REDUCE_ONLY", "HALTED"}:
        raise HTTPException(400, "invalid risk state")
    command_id = _enqueue_control_command(
        "risk_state_set",
        payload={"state": state, "reason": str(data.get("reason") or "web operator")},
    )
    return {"code": 0, "command_id": command_id, "msg": f"risk state {state} queued"}

@router.get("/engine/profiles")
async def api_engine_profiles(token: str = ""):
    require_admin_token(token)
    return {"code": 0, **_list_engine_profiles()}

@router.post("/engine/profiles")
async def api_create_engine_profile(data: dict, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    symbol = (data.get("symbol") or "").strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol required")
    slug = _profile_slug(data.get("slug") or symbol.split("-")[0])
    path, _enabled = _profile_find_path(slug)
    if path:
        raise HTTPException(400, f"profile exists: {slug}")
    _ensure_profile_uniqueness(slug, symbol)
    base_cfg = load_engine_config()
    cfg = _build_profile_config(base_cfg, data, slug, is_base=False)
    enabled = bool(data.get("enabled", True))
    target = _profile_enabled_path(slug) if enabled else _profile_disabled_path(slug)
    _write_yaml_file(target, cfg)
    return {"code": 0, "msg": "profile created", "slug": slug, "restart_required": True}

@router.put("/engine/profiles/{slug}")
async def api_update_engine_profile(slug: str, data: dict, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    slug = _profile_slug(slug)
    if slug == "base":
        before = load_engine_config()
        symbol = (data.get("symbol") or before.get("futures", {}).get("symbol", "")).strip().upper()
        cfg = _build_profile_config(before, dict(data, symbol=symbol), slug, is_base=True, prior=before)
        save_engine_config(cfg)
        history = _append_config_history("profile:base", before, cfg, source="multi_app")
        return {"code": 0, "msg": "base profile updated", "restart_required": True, "history": history}
    path, enabled = _profile_find_path(slug)
    if not path:
        raise HTTPException(404, "profile not found")
    before = _load_yaml_file(path)
    symbol = (data.get("symbol") or before.get("futures", {}).get("symbol", "")).strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol required")
    _ensure_profile_uniqueness(slug, symbol, current_slug=slug)
    cfg = _build_profile_config(load_engine_config(), dict(data, symbol=symbol), slug, is_base=False, prior=before)
    _write_yaml_file(path, cfg)
    history = _append_config_history(f"profile:{slug}", before, cfg, source="multi_app")
    return {"code": 0, "msg": "profile updated", "restart_required": True, "history": history}

@router.post("/engine/profiles/{slug}/enable")
async def api_enable_engine_profile(slug: str, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    slug = _profile_slug(slug)
    if slug == "base":
        raise HTTPException(400, "base profile is always enabled")
    path, enabled = _profile_find_path(slug)
    if not path:
        raise HTTPException(404, "profile not found")
    if enabled:
        return {"code": 0, "msg": "profile already enabled", "restart_required": False}
    target = _profile_enabled_path(slug)
    shutil.move(str(path), str(target))
    return {"code": 0, "msg": "profile enabled", "restart_required": True}

@router.post("/engine/profiles/{slug}/disable")
async def api_disable_engine_profile(slug: str, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    slug = _profile_slug(slug)
    if slug == "base":
        raise HTTPException(400, "base profile cannot be disabled")
    path, enabled = _profile_find_path(slug)
    if not path:
        raise HTTPException(404, "profile not found")
    if not enabled:
        return {"code": 0, "msg": "profile already disabled", "restart_required": False}
    target = _profile_disabled_path(slug)
    shutil.move(str(path), str(target))
    return {"code": 0, "msg": "profile disabled", "restart_required": True}

@router.delete("/engine/profiles/{slug}")
async def api_delete_engine_profile(slug: str, token: str = ""):
    require_admin_token(token)
    _deny_direct_control()
    slug = _profile_slug(slug)
    if slug == "base":
        raise HTTPException(400, "base profile cannot be deleted")
    path, _enabled = _profile_find_path(slug)
    if not path:
        raise HTTPException(404, "profile not found")
    os.remove(path)
    return {"code": 0, "msg": "profile deleted", "restart_required": True}


@router.get("/user/notifications")
async def api_get_notifications(token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token): raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    rows = db._conn.execute(
        "SELECT id,ts,title,content,strategy_id,is_read FROM notifications WHERE user_id=? ORDER BY ts DESC LIMIT 50",
        (uid,)
    ).fetchall()
    db.close()
    cols = ["id","ts","title","content","strategy_id","is_read"]
    return {"code": 0, "notifications": [dict(zip(cols, r)) for r in rows]}

@router.post("/user/notifications/{nid}/read")
async def api_read_notification(nid: int, token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token): raise HTTPException(401, "unauthorized")
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    db._conn.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (nid, get_user_id(token)))
    db._conn.commit(); db.close()
    return {"code": 0, "msg": "read"}

@router.get("/user/notifications/unread-count")
async def api_unread_count(token: str = ""):
    from auth import check_token, get_user_id
    if not check_token(token): raise HTTPException(401, "unauthorized")
    uid = get_user_id(token)
    from utils.database import Database
    db = Database(str(PROJ / "data" / "trading.db"))
    cnt = db._conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (uid,)
    ).fetchone()[0]
    db.close()
    return {"code": 0, "count": cnt}


@router.get("/app")
async def multi_app():
    from fastapi.responses import HTMLResponse
    html_path = Path(__file__).resolve().parent / "static" / "multi.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>App not found</h1>")
@router.get("/account")
async def get_account_status(token: str = ""):
    require_admin_token(token)
    primary = _primary_strategy_snapshot()
    monitor = primary.get("monitor", {}) if isinstance(primary, dict) else {}
    bal = {
        "eq": _safe_float_value(primary.get("cached_equity", 0)),
        "avail": _safe_float_value(primary.get("cached_avail", 0)),
        "frozen": _safe_float_value(monitor.get("cached_frozen", 0)),
    }
    pos = _projected_positions()
    pid = ""
    try: pid = open("data/run.pid").read().strip()
    except: pass
    data = {"pid":pid,"running":bool(pid),"balance":bal,"positions":pos}
    # Keep both the new nested envelope and the legacy top-level fields while
    # older browser bundles are still cached.
    return {"code":0,"data":data,"pid":pid,"running":bool(pid),"balance":bal,"positions":pos}

@router.get("/orders")
async def get_orders(token: str = ""):
    require_admin_token(token)
    return {"code": 0, "orders": _projected_live_orders()}
