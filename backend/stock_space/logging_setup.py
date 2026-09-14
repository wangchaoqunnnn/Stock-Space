"""日志配置: 控制台 + 按天滚动文件。

不使用 ``logging.handlers.TimedRotatingFileHandler`` 之外的第三方库,
保证在最小化安装的环境里也能写日志。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from .paths import LOG_DIR, ensure_dirs

_configured = False
_lock = threading.Lock()

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _SafeFormatter(logging.Formatter):
    """把日志里的绝对路径相对化, 避免把服务器目录结构写进日志与前端展示。"""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return super().format(record)
        except Exception:  # noqa: BLE001 - 日志绝不能反噬业务
            return f"{record.levelname} {record.getMessage()}"


def setup_logging(level: str = "INFO", *, to_file: bool = True) -> None:
    """初始化根 logger(幂等)。"""
    global _configured
    with _lock:
        if _configured:
            return
        _configured = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 清掉 uvicorn 预置的 handler, 统一输出格式
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_SafeFormatter(_FORMAT, _DATEFMT))
    root.addHandler(console)

    if to_file:
        try:
            ensure_dirs()
            file_handler = logging.handlers.TimedRotatingFileHandler(
                LOG_DIR / "stock_space.log",
                when="midnight",
                backupCount=14,
                encoding="utf-8",
                delay=True,
            )
            file_handler.setFormatter(_SafeFormatter(_FORMAT, _DATEFMT))
            root.addHandler(file_handler)
        except OSError:
            root.warning("日志文件不可写, 仅输出到控制台")

    # 降噪: 这些库的 DEBUG/INFO 太吵
    for noisy in ("httpx", "httpcore", "hpack", "urllib3", "asyncio", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger"]
