#!/usr/bin/env python3
"""hedge_kit 共享层：导入、基础类型 Layer/Segment 与工具函数。

由 futures_hedge.py 拆分而来，供各 mixin 复用，不参与任何交易逻辑。
"""
import os, time, json, math, tempfile
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional
from dataclasses import dataclass
from inventory.hedge_inventory import HedgeInventory, Pos
from utils.order_tracker import OrderTracker
from utils.execution_guard import ExecutionGuard
from execution.gateway import ExecutionGateway
from risk.account_state import AccountRiskState
from risk.policy import account_margin_mode, inventory_repair_hard_margin_override
from risk.position_limits import (
    dynamic_notional_limits,
    net_exposure_side_blocks,
    net_exposure_ratio,
    net_aware_margin_stop_ratio,
)
from strategies.hedge_calculations import (
    atr_adaptive_step,
    contract_qty_for_margin,
    hard_hedge_order_qty,
    inventory_net_size_multiplier,
)
from strategies.base import BaseStrategy
from utils.helpers import round_step, safe_float


def state_storage_path(config: dict, name: str) -> str:
    raw_dir = str((config or {}).get("user_data_dir") or "").strip()
    if raw_dir:
        path = Path(raw_dir)
        path.mkdir(parents=True, exist_ok=True)
        return str(path / name)
    return str(Path("data") / name)


@dataclass
class Layer:
    """单个加仓层"""
    price: float = 0.0
    qty: float = 0.0
    cost: float = 0.0
    ts: float = 0.0

@dataclass
class Segment:
    """分段池 - 连续同向加仓集合"""
    side: str = ""
    layers: list = None
    total_qty: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    close_price: float = 0.0
    close_order_id: str = ""
    buffer_hit: bool = False
    
    def __post_init__(self):
        if self.layers is None:
            self.layers = []
