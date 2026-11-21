"""统一日志系统 + 日志轮转"""
"""
"""

import os, sys, logging
from logging.handlers import RotatingFileHandler


def init_logger(config: dict) -> logging.Logger:
    log_conf = config.get("logging", {})
    level = getattr(logging, log_conf.get("level", "INFO").upper(), logging.INFO)
    log_file = log_conf.get("file", "logs/trading.log")
    max_mb = log_conf.get("max_size_mb", 50)
    backup = log_conf.get("backup_count", 7)
    console = log_conf.get("console", True)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger("okx_quant")
    logger.setLevel(level)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = RotatingFileHandler(log_file, maxBytes=max_mb*1024*1024,
                              backupCount=backup, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    return logger


def get_logger(name=None):
    if name:
        return logging.getLogger(f"okx_quant.{name}")
    return logging.getLogger("okx_quant")
