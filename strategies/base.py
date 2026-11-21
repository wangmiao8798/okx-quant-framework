"""
策略基类
"""
from abc import ABC, abstractmethod
from typing import Dict, Any
from utils.logger import get_logger

class BaseStrategy(ABC):
    def __init__(self, config: dict, name: str = ""):
        self.log = get_logger(f"strategy.{name}")
        self._running = False
        self.config = config
        self.name = name
        self._running = False
        self._enabled = True

    @abstractmethod
    def on_tick(self, ticker: dict) -> dict: pass

    @abstractmethod
    def on_order_result(self, result: dict): pass

    @abstractmethod
    def get_status(self) -> Dict[str, Any]: pass

    def start(self):
        self._running = True
        self.log.info(f"策略 [{self.name}] 已启动")

    def stop(self):
        self._running = False
        self.log.info(f"策略 [{self.name}] 已停止")

    @property
    def is_running(self): return self._running

    @property
    def is_enabled(self): return self._enabled

    def set_enabled(self, e): self._enabled = e

    def update_moca(self, params: dict):
        """更新MOCA自适应参数（各策略自行实现）"""
        pass
