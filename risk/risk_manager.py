from dataclasses import dataclass
from utils.logger import get_logger
from risk.black_swan import BlackSwanProtector
from risk.liquidation_guard import LiquidationGuard
from risk.total_loss_guard import TotalLossGuard
from execution.gateway import ExecutionGateway

@dataclass
class RiskStatus:
    frozen: bool = False
    cooldown: float = 0
    liq: dict = None
    safe: bool = True

class RiskManager:
    def __init__(self, config, client=None, execution_gateway=None):
        self.log = get_logger("risk.manager")
        self._cfg = config; self._client = client
        self._execution_gateway = execution_gateway or (ExecutionGateway(client) if client else None)
        self._safe = True; self._reason = ""
        self.black_swan = BlackSwanProtector(config, on_trigger=self._bs)
        self.liquidation = LiquidationGuard(
            config, on_lev=self._rl, on_pos=self._rp, on_emer=self._re
        )
        self.total_loss = TotalLossGuard(config, on_stop=self._tl)

    def _resume(self, reason):
        if self._reason == reason:
            self._safe = True
            self._reason = ""
            self.log.info(f"风控: {reason} 冷却结束，恢复交易")

    def _mark_failure(self, reason: str, exc) -> None:
        self._safe = False
        self._reason = reason
        self.log.error(f"风控动作失败: {reason}: {exc}")

    def _bs(self):
        self._safe=False; self._reason="black_swan"
        self.log.warning("风控: 异常行情，仅暂停新开仓")

    def _rl(self,lev):
        if not self._execution_gateway: return
        try:
            s=self._cfg.get("futures",{}).get("symbol","BTC-USDT-SWAP")
            response = self._execution_gateway.set_leverage(s,lev)
            if isinstance(response, dict) and str(response.get("code", "0")) != "0":
                raise RuntimeError(response)
        except Exception as exc:
            self._mark_failure("leverage_update_failed", exc)
        else:
            self._resume("leverage_update_failed")

    def _rp(self,ratio):
        if not self._client or not self._execution_gateway: return
        try:
            s=self._cfg.get("futures",{}).get("symbol","BTC-USDT-SWAP")
            positions = self._client.get_positions(s)
            for p in positions:
                sz=float(p.get("pos",0))
                if sz<=0: continue
                rs=str(round(sz*ratio,4))
                sd="sell" if p["posSide"]=="long" else "buy"
                response = self._execution_gateway.place_order(s,sd,rs,pos_side=p["posSide"],reduce_only=True)
                if isinstance(response, dict):
                    code = response.get("sCode", response.get("code", "0"))
                    if str(code) != "0":
                        raise RuntimeError(response)
        except Exception as exc:
            self._mark_failure("emergency_reduce_failed", exc)
        else:
            self._resume("emergency_reduce_failed")

    def _re(self):
        self._safe=False; self._reason="emergency"
    def _tl(self):
        self._safe=False; self._reason="total_loss"
        self.log.warning("风控: 全局止损触发")
        self.log.warning("风控: 紧急全平!")
        if self._execution_gateway:
            try:
                response = self._execution_gateway.close_all("")
                if isinstance(response, dict) and str(response.get("code", "0")) != "0":
                    raise RuntimeError(response)
            except Exception as exc:
                self._mark_failure("emergency_close_failed", exc)

    def check_all(self, risk, lev, price, prev):
        st=RiskStatus()
        self.black_swan.check(price,prev)
        if self.black_swan.is_frozen:
            self.black_swan.check_thaw()
        if self._client and not self.black_swan.is_frozen:
            try:
                eq = self._client.get_total_equity()
                self.total_loss.check(eq)
            except Exception as exc:
                self._mark_failure("account_data_unavailable", exc)
            else:
                self._resume("account_data_unavailable")
            self.liquidation.check_and_act(risk,lev)
        if not self.black_swan.is_frozen:
            self._resume("black_swan")
        if self.total_loss.halted:
            self._safe = False
            self._reason = self.total_loss.halt_reason or "total_loss"
        else:
            self._resume("total_loss")
            self._resume("account_data_unavailable")
        st.liq=self.liquidation.stats()
        st.frozen=self.black_swan.is_frozen or self.total_loss.halted or self._reason == "emergency"
        st.cooldown=max(self.black_swan.remaining, self.total_loss.remaining)
        st.safe=self._safe and not st.frozen
        return st

    @property
    def halted(self): return not self._safe
    @property
    def halt_reason(self): return self._reason

    def reset(self):
        self._safe=True; self._reason=""
        self.black_swan.reset()
