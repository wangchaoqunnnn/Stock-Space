"""核心基础设施。

依赖方向(严格单向, 避免循环导入):

    core  ->  providers  ->  engines  ->  services  ->  api

``core`` 只包含路径、缓存、内存护栏、时间与代码工具以及通用 HTTP 客户端,
不导入任何业务模块, 因此可以被任意层安全引用。
"""

from __future__ import annotations

from .cache import BoundedTTLCache, MultiCache
from .memory import memory_guard, read_rss_mb, read_system_memory
from .util import (
    CN_TZ,
    MAJOR_INDICES,
    board_of,
    detect_market,
    limit_pct,
    market_session,
    normalize_code,
    now_cn,
    secid,
    to_symbol,
    today_str,
)

__all__ = [
    "BoundedTTLCache",
    "MultiCache",
    "memory_guard",
    "read_rss_mb",
    "read_system_memory",
    "CN_TZ",
    "MAJOR_INDICES",
    "board_of",
    "detect_market",
    "limit_pct",
    "market_session",
    "normalize_code",
    "now_cn",
    "secid",
    "to_symbol",
    "today_str",
]
