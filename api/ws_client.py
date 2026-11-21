"""
WebSocket 客户端（预留）
当前使用 REST 轮询模式
"""
from utils.logger import get_logger

class WSClient:
    def __init__(self, config, credentials=None):
        self.log = get_logger("api.ws")
        self.log.info("WS 客户端（预留模式）")

    def start(self): pass
    def stop(self): pass
    @property
    def is_connected(self): return False
