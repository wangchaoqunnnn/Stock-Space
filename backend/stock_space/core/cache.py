"""有界 TTL 缓存。

三件事必须同时成立, 否则长跑的服务一定会内存膨胀:
  1. **条目上限**: 超过上限按"最久未使用"淘汰, 不依赖 TTL 兜底;
  2. **TTL**: 过期的键在读取与巡检时被清理;
  3. **统一登记**: 所有缓存注册到内存护栏(``memory.py``), 便于一键压缩/清空。

统一使用单调时钟(``time.monotonic``)判断过期, 不受系统时间调整影响。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Generic, Iterable, TypeVar

T = TypeVar("T")


@dataclass
class CacheStats:
    name: str
    size: int
    max_entries: int
    ttl_seconds: float
    hits: int
    misses: int
    expired_purged: int
    lru_evicted: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl_seconds,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
            "expired_purged": self.expired_purged,
            "lru_evicted": self.lru_evicted,
            "usage_pct": round(100.0 * self.size / self.max_entries, 2) if self.max_entries else 0.0,
        }


class BoundedTTLCache(Generic[T]):
    """带容量上限与 TTL 的线程安全缓存。"""

    __slots__ = (
        "name", "_max_entries", "ttl_seconds", "_data",
        "_lock", "_hits", "_misses", "_expired", "_evicted",
    )

    def __init__(self, name: str, max_entries: int = 1024, ttl_seconds: float = 60.0) -> None:
        self.name = name
        self._max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._data: "OrderedDict[str, tuple[float, T]]" = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._expired = 0
        self._evicted = 0

    # ------------------------------ 基本属性 ------------------------------
    @property
    def size(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @max_entries.setter
    def max_entries(self, value: int) -> None:
        with self._lock:
            self._max_entries = max(1, int(value))
            self._enforce_capacity()

    # ------------------------------ 读写 ------------------------------
    def get(self, key: str, default: T | None = None) -> T | None:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return default
            expires_at, value = item
            if expires_at and expires_at <= now:
                del self._data[key]
                self._expired += 1
                self._misses += 1
                return default
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def peek(self, key: str) -> T | None:
        """只读不计数(用于健康检查等旁路读取)。"""
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at and expires_at <= now:
                return None
            return value

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        ttl_value = self.ttl_seconds if ttl is None else max(0.0, float(ttl))
        expires_at = (time.monotonic() + ttl_value) if ttl_value > 0 else 0.0
        with self._lock:
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            self._enforce_capacity()

    def get_or_set(self, key: str, factory, ttl: float | None = None) -> T:
        """读穿缓存。``factory`` 只在未命中时调用(持锁外调用, 避免长任务阻塞缓存)。"""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        if value is not None:
            self.set(key, value, ttl)
        return value

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())

    def items(self) -> list[tuple[str, T]]:
        now = time.monotonic()
        with self._lock:
            out: list[tuple[str, T]] = []
            for key, (expires_at, value) in self._data.items():
                if expires_at and expires_at <= now:
                    continue
                out.append((key, value))
            return out

    # ------------------------------ 治理 ------------------------------
    def _enforce_capacity(self) -> None:
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)
            self._evicted += 1

    def purge_expired(self) -> int:
        now = time.monotonic()
        with self._lock:
            dead = [k for k, (exp, _) in self._data.items() if exp and exp <= now]
            for key in dead:
                del self._data[key]
            self._expired += len(dead)
            return len(dead)

    def trim_to(self, ratio: float) -> int:
        """按比例保留最新的条目(内存吃紧时由内存护栏调用)。"""
        keep = max(1, int(len(self._data) * max(0.0, min(1.0, ratio))))
        removed = 0
        with self._lock:
            while len(self._data) > keep:
                self._data.popitem(last=False)
                removed += 1
                self._evicted += 1
        return removed

    def clear(self) -> int:
        with self._lock:
            count = len(self._data)
            self._data.clear()
            return count

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                name=self.name,
                size=len(self._data),
                max_entries=self._max_entries,
                ttl_seconds=self.ttl_seconds,
                hits=self._hits,
                misses=self._misses,
                expired_purged=self._expired,
                lru_evicted=self._evicted,
            )


class MultiCache:
    """把多个命名缓存聚合起来, 便于按业务域做一次清理。"""

    def __init__(self, caches: Iterable[BoundedTTLCache]) -> None:
        self._caches = list(caches)

    def clear(self) -> int:
        return sum(c.clear() for c in self._caches)

    def purge_expired(self) -> int:
        return sum(c.purge_expired() for c in self._caches)

    def trim_to(self, ratio: float) -> int:
        return sum(c.trim_to(ratio) for c in self._caches)

    @property
    def total_entries(self) -> int:
        return sum(c.size for c in self._caches)


__all__ = ["BoundedTTLCache", "CacheStats", "MultiCache"]
