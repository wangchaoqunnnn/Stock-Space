"""内存监控与缓存护栏。

对应需求「你需要能监控内存情况」。三层能力:

1. **采集**: 每 ``interval`` 秒采样进程 RSS(优先 ``psutil``, 其次 ``/proc/self/statm``,
   再次 Windows ``psapi``, 最后 ``resource``), 记录有界环形历史(默认 720 点 ≈ 1 小时)。
2. **展示**: ``report()`` 输出当前值 / 峰值 / 系统总量与占用率 / 趋势序列 / 各缓存占用,
   由 ``/api/system/metrics`` 与前端「内存监控」页面消费。
3. **治理**: 达到软上限压缩缓存到 50% 并 ``gc.collect()``; 达到硬上限清空全部缓存。
   同时记录触发次数 —— 长期持续触发说明配置不合理, 会在健康检查里暴露出来。

RSS 读取按平台分支, 只依赖标准库(psutil 缺失时自动降级)。
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

from .cache import BoundedTTLCache

logger = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0


# --------------------------------------------------------------------------- #
# RSS 采集
# --------------------------------------------------------------------------- #
_PSUTIL_PROC: Any = None
_PSUTIL_TRIED = False


def _rss_from_psutil() -> float:
    """用 psutil 读取 RSS。``Process`` 对象缓存复用 —— 每次采样都新建的话,
    在 10 秒一次的长期运行下会明显增加开销。"""
    global _PSUTIL_PROC, _PSUTIL_TRIED
    if _PSUTIL_PROC is None:
        if _PSUTIL_TRIED:
            return 0.0
        _PSUTIL_TRIED = True
        try:
            import psutil  # type: ignore

            _PSUTIL_PROC = psutil.Process(os.getpid())
        except Exception:  # noqa: BLE001 - 未安装即降级
            _PSUTIL_PROC = None
            return 0.0
    try:
        return _PSUTIL_PROC.memory_info().rss / _MB
    except Exception:  # noqa: BLE001
        return 0.0


def _rss_from_proc() -> float:
    """Linux / Docker 容器主路径。"""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            parts = fh.read().split()
        if len(parts) >= 2:
            return int(parts[1]) * 4096 / _MB
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _rss_from_win32() -> float:
    """Windows 兜底(必须显式声明 restype/argtypes, 否则 64 位下句柄被截断)。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.restype = wintypes.BOOL
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        if fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            value = counters.WorkingSetSize / _MB
            if value > 0:
                return value
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _rss_from_resource() -> float:
    try:
        import resource  # type: ignore

        usage = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if usage > 0:
            return usage / 1024.0  # Linux: KB; macOS 上偏大但仅作兜底
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def read_rss_mb() -> float:
    """读取当前进程常驻内存(MB)。全部手段失败时返回 0.0。"""
    for reader in (_rss_from_psutil, _rss_from_proc, _rss_from_win32, _rss_from_resource):
        try:
            value = reader()
        except Exception:  # noqa: BLE001
            continue
        if value and value > 0:
            return value
    return 0.0


def read_system_memory() -> dict[str, Any]:
    """整机内存信息(供"内存监控"页面显示占用率)。"""
    out: dict[str, Any] = {"total_mb": 0.0, "available_mb": 0.0, "used_pct": 0.0, "source": "none"}
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        out.update(
            total_mb=round(vm.total / _MB, 1),
            available_mb=round(vm.available / _MB, 1),
            used_pct=round(vm.percent, 1),
            source="psutil",
        )
        return out
    except Exception:  # noqa: BLE001
        pass
    # Linux 兜底
    try:
        info: dict[str, float] = {}
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = float(rest.strip().split()[0]) / 1024.0
        total = info.get("MemTotal", 0.0)
        avail = info.get("MemAvailable", 0.0)
        if total:
            out.update(
                total_mb=round(total, 1),
                available_mb=round(avail, 1),
                used_pct=round(100.0 * (total - avail) / total, 1),
                source="procfs",
            )
    except Exception:  # noqa: BLE001
        pass
    return out


def read_container_limit_mb() -> float | None:
    """读取 cgroup 内存上限(容器部署时才知道自己的天花板)。"""
    for candidate in (
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    ):
        try:
            with open(candidate, "r", encoding="ascii") as fh:
                raw = fh.read().strip()
            if raw == "max":
                return None
            value = int(raw)
            if 0 < value < (1 << 62):
                return value / _MB
        except (OSError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------- #
# 采样与护栏
# --------------------------------------------------------------------------- #
@dataclass
class MemorySample:
    ts: float
    rss_mb: float
    entries: int

    def as_dict(self) -> dict[str, Any]:
        return {"ts": round(self.ts, 1), "rss_mb": round(self.rss_mb, 2), "cache_entries": self.entries}


@dataclass
class MemoryState:
    current_rss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    soft_breaches: int = 0
    hard_breaches: int = 0
    last_action: str = "none"
    last_action_ts: float = 0.0
    total_expired_purged: int = 0
    total_evicted: int = 0
    gc_collections: int = 0
    started: bool = False


class _Trimable(Protocol):
    name: str

    @property
    def size(self) -> int: ...

    def purge_expired(self) -> int: ...

    def trim_to(self, ratio: float) -> int: ...

    def clear(self) -> int: ...

    def stats(self): ...


class MemoryGuard:
    """进程内存监控 + 缓存总量治理。"""

    def __init__(self) -> None:
        self._caches: dict[str, _Trimable] = {}
        self._lock = threading.RLock()
        self.state = MemoryState()
        self._history: deque[MemorySample] = deque(maxlen=720)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.soft_limit_mb = 800.0
        self.hard_limit_mb = 1400.0
        self.interval = 10.0
        self.container_limit_mb: float | None = read_container_limit_mb()

    # ------------------------------- 配置 -------------------------------
    def configure(
        self,
        *,
        soft_limit_mb: float | None = None,
        hard_limit_mb: float | None = None,
        interval: float | None = None,
    ) -> None:
        with self._lock:
            if soft_limit_mb and soft_limit_mb > 0:
                self.soft_limit_mb = float(soft_limit_mb)
            if hard_limit_mb and hard_limit_mb > 0:
                self.hard_limit_mb = float(hard_limit_mb)
            if interval and interval > 0:
                self.interval = float(interval)
            # 容器内存上限是硬约束: 自动收紧阈值, 避免被 OOM Killer 干掉
            if self.container_limit_mb:
                self.soft_limit_mb = min(self.soft_limit_mb, self.container_limit_mb * 0.72)
                self.hard_limit_mb = min(self.hard_limit_mb, self.container_limit_mb * 0.88)

    # ------------------------------- 注册 -------------------------------
    def register(self, cache: _Trimable) -> _Trimable:
        with self._lock:
            self._caches[cache.name] = cache
        return cache

    def register_all(self, caches: list[_Trimable]) -> None:
        for cache in caches:
            self.register(cache)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._caches.pop(name, None)

    def all_caches(self) -> list[_Trimable]:
        with self._lock:
            return list(self._caches.values())

    @property
    def total_entries(self) -> int:
        return sum(c.size for c in self.all_caches())

    # ------------------------------ 生命周期 ------------------------------
    def start(self) -> None:
        with self._lock:
            if self.state.started:
                return
            self.state.started = True
        self.sample_and_enforce()  # 立即采一次, 前端首屏就有数
        self._thread = threading.Thread(target=self._loop, name="memory-guard", daemon=True)
        self._thread.start()
        logger.info(
            "内存护栏已启动: soft=%.0fMB hard=%.0fMB interval=%.0fs 容器上限=%s",
            self.soft_limit_mb,
            self.hard_limit_mb,
            self.interval,
            f"{self.container_limit_mb:.0f}MB" if self.container_limit_mb else "未检测到",
        )

    def stop(self) -> None:
        self._stop.set()
        self.state.started = False
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample_and_enforce()
            except Exception:  # noqa: BLE001 - 监控线程不允许因异常退出
                logger.exception("内存监控循环异常(已忽略)")

    # ------------------------------ 采样 ------------------------------
    def sample_and_enforce(self) -> MemorySample:
        rss = read_rss_mb()
        entries = self.total_entries

        self.state.current_rss_mb = rss
        self.state.peak_rss_mb = max(self.state.peak_rss_mb, rss)
        sample = MemorySample(ts=time.time(), rss_mb=rss, entries=entries)
        self._history.append(sample)

        purged = 0
        for cache in self.all_caches():
            try:
                purged += cache.purge_expired()
            except Exception:  # noqa: BLE001
                continue
        self.state.total_expired_purged += purged

        if rss >= self.hard_limit_mb:
            self.state.hard_breaches += 1
            freed = self._apply("hard_flush", 0.0)
            self.state.last_action = f"hard_flush(freed={freed})"
            self.state.last_action_ts = time.time()
            self.state.total_evicted += freed
            collected = gc.collect()
            self.state.gc_collections += 1
            logger.error(
                "内存达到硬上限 %.0fMB >= %.0fMB: 已清空全部缓存 %d 条, gc 回收 %d 个对象",
                rss, self.hard_limit_mb, freed, collected,
            )
        elif rss >= self.soft_limit_mb:
            self.state.soft_breaches += 1
            freed = self._apply("soft_trim", 0.5)
            self.state.last_action = f"soft_trim(freed={freed})"
            self.state.last_action_ts = time.time()
            self.state.total_evicted += freed
            gc.collect()
            self.state.gc_collections += 1
            logger.warning(
                "内存达到软上限 %.0fMB >= %.0fMB: 已压缩缓存释放 %d 条",
                rss, self.soft_limit_mb, freed,
            )
        elif purged:
            self.state.last_action = f"ttl_purge({purged})"
            self.state.last_action_ts = time.time()

        return sample

    def _apply(self, mode: str, ratio: float) -> int:
        freed = 0
        for cache in self.all_caches():
            try:
                before = cache.size
                if mode == "hard_flush":
                    cache.clear()
                else:
                    cache.trim_to(ratio)
                freed += max(0, before - cache.size)
            except Exception:  # noqa: BLE001
                continue
        return freed

    # ------------------------------ 手动操作 ------------------------------
    def flush(self) -> dict[str, Any]:
        """手动释放缓存(前端「内存监控」页面的"立即释放"按钮)。"""
        freed = self._apply("hard_flush", 0.0)
        before = self.state.current_rss_mb
        collected = gc.collect()
        self.state.total_evicted += freed
        self.state.gc_collections += 1
        self.state.last_action = f"manual_flush(freed={freed})"
        self.state.last_action_ts = time.time()
        after = read_rss_mb()
        self.state.current_rss_mb = after
        self.state.peak_rss_mb = max(self.state.peak_rss_mb, after)
        return {
            "freed_entries": freed,
            "gc_collected": collected,
            "rss_before_mb": round(before, 2),
            "rss_after_mb": round(after, 2),
        }

    # ------------------------------ 报告 ------------------------------
    def report(self, trend_points: int = 120) -> dict[str, Any]:
        st = self.state
        # 护栏未启动或尚未采样时，指标接口也应返回真实读数（而不是 0）
        current_rss = st.current_rss_mb
        if current_rss <= 0:
            current_rss = read_rss_mb()
            st.current_rss_mb = current_rss
            st.peak_rss_mb = max(st.peak_rss_mb, current_rss)
        hist = list(self._history)[-max(1, trend_points):]
        system = read_system_memory()
        limit = self.container_limit_mb
        return {
            "process": {
                "pid": os.getpid(),
                "rss_mb": round(current_rss, 2),
                "peak_rss_mb": round(max(st.peak_rss_mb, current_rss), 2),
                "rss_delta_mb": round(
                    (hist[-1].rss_mb - hist[0].rss_mb) if len(hist) > 1 else 0.0, 2
                ),
                "threads": threading.active_count(),
            },
            "limits": {
                "soft_limit_mb": round(self.soft_limit_mb, 1),
                "hard_limit_mb": round(self.hard_limit_mb, 1),
                "container_limit_mb": round(limit, 1) if limit else None,
                "soft_usage_pct": round(100.0 * current_rss / self.soft_limit_mb, 2)
                if self.soft_limit_mb else 0.0,
                "hard_usage_pct": round(100.0 * current_rss / self.hard_limit_mb, 2)
                if self.hard_limit_mb else 0.0,
            },
            "system": system,
            "guard": {
                "running": st.started,
                "interval_seconds": self.interval,
                "soft_breaches": st.soft_breaches,
                "hard_breaches": st.hard_breaches,
                "gc_collections": st.gc_collections,
                "last_action": st.last_action,
                "last_action_ago_seconds": round(time.time() - st.last_action_ts, 1)
                if st.last_action_ts else None,
                "total_ttl_purged": st.total_expired_purged,
                "total_evicted": st.total_evicted,
            },
            "caches": {
                "total_entries": self.total_entries,
                "items": [c.stats().as_dict() for c in self.all_caches()],
            },
            "trend": [s.as_dict() for s in hist],
        }

    def log_line(self) -> str:
        st = self.state
        return (
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} rss={st.current_rss_mb:.1f}MB "
            f"peak={st.peak_rss_mb:.1f}MB caches={self.total_entries} "
            f"soft={st.soft_breaches} hard={st.hard_breaches} action={st.last_action}"
        )


#: 全局单例
memory_guard = MemoryGuard()


__all__ = [
    "memory_guard",
    "MemoryGuard",
    "read_rss_mb",
    "read_system_memory",
    "read_container_limit_mb",
]
