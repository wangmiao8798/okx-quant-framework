"""State container and pure calculations for a two-sided hedge inventory."""

from dataclasses import dataclass


@dataclass
class Pos:
    side: str
    entry: float = 0.0
    qty: float = 0.0
    cost: float = 0.0
    adds: int = 0


class HedgeInventory:
    def __init__(self):
        self.long = Pos("long")
        self.short = Pos("short")

    def net_qty(self) -> float:
        return self.long.qty - self.short.qty

    def gross_qty(self) -> float:
        return self.long.qty + self.short.qty

    def imbalance_ratio(self) -> float:
        gross = self.gross_qty()
        return abs(self.net_qty()) / gross if gross > 0 else 0.0

    def dominant_side(self, minimum_qty: float = 0.0) -> str:
        if self.long.qty > self.short.qty + minimum_qty:
            return "long"
        if self.short.qty > self.long.qty + minimum_qty:
            return "short"
        return ""

    def repair_plan(
        self,
        price: float,
        contract_value: float,
        max_net_notional: float,
        imbalance_gate: float = 0.70,
        stage_ratio: float = 0.30,
        repair_mode: bool = False,
        exit_net_notional: float | None = None,
        exit_imbalance_ratio: float | None = None,
    ) -> dict:
        price_value = max(0.0, float(price or 0.0) * float(contract_value or 0.0))
        net_qty = self.net_qty()
        net_notional = abs(net_qty) * price_value
        imbalance = self.imbalance_ratio()
        dominant = self.dominant_side()
        entry_net = max(0.0, float(max_net_notional or 0.0))
        entry_imbalance = max(0.0, float(imbalance_gate))
        exit_net = entry_net if exit_net_notional is None else max(0.0, float(exit_net_notional))
        exit_imbalance = entry_imbalance if exit_imbalance_ratio is None else max(0.0, float(exit_imbalance_ratio))
        entry_triggered = net_notional > entry_net or imbalance >= entry_imbalance
        exit_ready = net_notional <= exit_net and imbalance <= exit_imbalance
        active = bool(
            price_value > 0
            and dominant
            and (not exit_ready if repair_mode else entry_triggered)
        )
        proposed_qty = 0.0
        if active:
            dominant_qty = self.long.qty if dominant == "long" else self.short.qty
            weak_qty = self.short.qty if dominant == "long" else self.long.qty
            net_repair_qty = max(0.0, net_notional - exit_net) / price_value
            imbalance_repair_qty = max(
                0.0,
                (dominant_qty - weak_qty - exit_imbalance * (dominant_qty + weak_qty))
                / max(1.0 + exit_imbalance, 1e-8),
            )
            required_qty = max(net_repair_qty, imbalance_repair_qty)
            proposed_qty = min(abs(net_qty), dominant_qty * max(0.0, float(stage_ratio)), required_qty)
        return {
            "active": active and proposed_qty > 0,
            "repair_mode": bool(repair_mode),
            "entry_triggered": bool(entry_triggered),
            "exit_ready": bool(exit_ready),
            "dominant_side": dominant,
            "repair_side": "short" if dominant == "long" else "long" if dominant == "short" else "",
            "imbalance_ratio": imbalance,
            "net_notional": net_notional,
            "proposed_qty": proposed_qty,
        }
