"""异步扫描任务：把分钟级的策略扫描从 HTTP 请求里挪到后台任务。

为什么必须这么做（真实事故）：

    ``POST /api/strategies/{key}/scan`` 是**同步**执行的。一次全市场扫描要评估
    约 5900 只标的并逐只拉日线，实测单个策略耗时 **158 秒**（响应体 383KB）。
    在这 158 秒里这条 HTTP 连接一直空转，任何一环都可能把它掐断：
    反向代理的 read timeout、浏览器/系统的空闲连接回收、用户切走页面、
    笔记本休眠。连接一断，浏览器拿到的是网络层失败，前端只能显示
    ``api.js`` 里那句兜底文案 —— "无法连接到后端服务。请确认服务已启动"。

    这就是用户看到的「扫描失败」：后端其实是好的、策略也确实在跑，
    只是**同步长请求这种形状本身就是错的**。

改成两步之后，每个 HTTP 请求都在毫秒级返回：

    1. ``POST /api/scan/jobs``        -> 立刻返回 job_id，扫描在后台跑
    2. ``GET  /api/scan/jobs/{id}``   -> 前端轮询进度，完成时带回完整结果

附带好处：
  * 轮询天然就是进度条的数据源（已完成/总数），用户能看到"跑到哪了"；
  * 任务与连接解耦 —— 用户切走页面再回来，扫描照常完成，结果还在；
  * 并发受 ``quotas.max_concurrent_scan_jobs`` 限制，避免多个扫描同时
    打满上游数据源。

任务状态保存在进程内存（栈上无状态依赖）：应用重启后历史任务丢失，
但扫描结果本身已通过 ``persist=True`` 落库，可从 ``/last`` 取回。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import config
from ..engines import STRATEGY_ORDER, get as get_strategy
from ..engines.scanner import scan, scan_many
from ..store.settings_store import settings_store

logger = logging.getLogger(__name__)

#: 终态：不再变化，轮询到这些状态即可停止
TERMINAL = ("succeeded", "failed", "cancelled")
#: 保留多少个历史任务供查询（防止长时间运行后内存无限增长）
MAX_HISTORY = 40


@dataclass
class ScanJob:
    """一次扫描任务的可观测状态。"""

    id: str
    kind: str                      #: 'all' 或策略 key
    status: str = "queued"         #: queued/running/succeeded/failed/cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    params: dict[str, Any] = field(default_factory=dict)
    total: int = 0                 #: 需要扫描的策略数
    done: int = 0                  #: 已完成的策略数
    current: str = ""              #: 当前正在跑的策略 key
    detail: str = ""               #: 细粒度进度说明（如"已评估 1200/4459 只"）
    percent_override: float | None = None   #: 单策略模式下的细粒度进度
    result: Any = None             #: 成功时的业务结果
    error: str = ""                #: 失败原因

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - (self.started_at or self.created_at))

    @property
    def percent(self) -> float:
        if self.status in TERMINAL:
            return 100.0
        if self.percent_override is not None:
            return self.percent_override
        if self.total <= 0:
            return 0.0
        return round(min(99.0, self.done / self.total * 100.0), 1)

    def as_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "percent": self.percent,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "detail": self.detail,
            "elapsed_seconds": round(self.elapsed, 1),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
        if include_result:
            payload["result"] = self.result
        return payload


class ScanJobService:
    """进程内的扫描任务注册表 + 执行器。"""

    def __init__(self) -> None:
        self._jobs: dict[str, ScanJob] = {}
        self._order: list[str] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: 同一时刻最多几个扫描任务（0 表示不限制）
        self._sem: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------ 并发闸门
    def _semaphore(self) -> asyncio.Semaphore:
        limit = int(config().get("quotas.max_concurrent_scan_jobs", 2) or 2)
        if self._sem is None or getattr(self._sem, "_ss_limit", None) != limit:
            sem = asyncio.Semaphore(max(1, limit))
            sem._ss_limit = limit  # type: ignore[attr-defined]
            self._sem = sem
        return self._sem

    # ------------------------------------------------------------------ 查询
    def get(self, job_id: str) -> ScanJob | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """最近的任务（不含大结果体，避免列表接口体积失控）。"""
        ids = list(reversed(self._order))[: max(1, limit)]
        return [self._jobs[i].as_dict(include_result=False) for i in ids if i in self._jobs]

    def active(self) -> list[str]:
        return [j.id for j in self._jobs.values() if j.status in ("queued", "running")]

    # ------------------------------------------------------------------ 创建
    def _register(self, kind: str, params: dict[str, Any], total: int) -> ScanJob:
        job = ScanJob(id=uuid.uuid4().hex[:16], kind=kind, params=params, total=total)
        self._jobs[job.id] = job
        self._order.append(job.id)
        self._trim()
        return job

    def _trim(self) -> None:
        while len(self._order) > MAX_HISTORY:
            old = self._order.pop(0)
            if self._jobs.get(old) and self._jobs[old].status in TERMINAL:
                self._jobs.pop(old, None)
            else:
                #: 未结束的任务放回队尾，不能丢
                self._order.append(old)
                break

    def start_strategy(
        self, key: str, *, params: dict[str, Any] | None = None,
        refresh: bool = False, limit: int = 200, persist: bool = True,
    ) -> ScanJob:
        if get_strategy(key) is None:
            raise ValueError(f"未知策略: {key}")
        merged = {**(settings_store.all_strategy_params().get(key) or {}), **(params or {})}
        job = self._register(key, merged, total=1)
        self._spawn(job, self._run_strategy(job, key, merged, refresh, limit, persist))
        return job

    def start_all(
        self, *, refresh: bool = False, per_strategy: int = 20, persist: bool = True,
    ) -> ScanJob:
        params = settings_store.all_strategy_params()
        job = self._register("all", params, total=len(STRATEGY_ORDER))
        self._spawn(job, self._run_all(job, params, refresh, per_strategy, persist))
        return job

    def _spawn(self, job: ScanJob, coro: Any) -> None:
        task = asyncio.create_task(self._guarded(job, coro), name=f"scan-job-{job.id}")
        #: 强引用住 task，否则可能被 GC 掉导致任务莫名其妙消失
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: self._tasks.pop(jid, None))

    async def _guarded(self, job: ScanJob, coro: Any) -> None:
        """统一收尾：无论成功失败都写入终态，绝不让任务卡在 running。"""
        async with self._semaphore():
            job.status = "running"
            job.started_at = time.time()
            logger.info("扫描任务 %s(%s) 开始", job.id, job.kind)
            try:
                job.result = await coro
                job.status = "succeeded"
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.error = "任务已取消（服务正在关闭）"
                raise
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                logger.exception("扫描任务 %s(%s) 失败", job.id, job.kind)
            finally:
                job.done = job.total
                job.current = ""
                job.detail = ""
                #: 清掉细粒度覆盖值，让终态统一由 status 决定为 100%
                job.percent_override = None
                job.finished_at = time.time()
                logger.info(
                    "扫描任务 %s(%s) 结束: %s 用时 %.1fs",
                    job.id, job.kind, job.status, job.elapsed,
                )

    # ------------------------------------------------------------------ 执行体
    async def _run_strategy(
        self, job: ScanJob, key: str, params: dict[str, Any],
        refresh: bool, limit: int, persist: bool,
    ) -> dict[str, Any]:
        job.current = key

        def on_progress(done: int, total: int) -> None:
            #: 单策略模式下 percent 用"已评估标的数 / 全市场标的数"，
            #: 否则进度条会在 2~3 分钟里一直停在 0%，用户无法区分"在跑"和"卡死"。
            if total > 0:
                job.percent_override = round(min(99.0, done / total * 100.0), 1)
            job.detail = f"已评估 {done}/{total} 只"

        outcome = await scan(
            key, params=params, force=refresh, limit=limit, persist=persist,
            on_progress=on_progress,
        )
        job.done = 1
        payload = outcome.as_dict(limit=limit)
        if persist and outcome.signals:
            try:
                from . import user_service

                await asyncio.to_thread(
                    user_service.log_signals, outcome.signals, strategy=key,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("信号流水写入失败: %s", exc)
        return payload

    async def _run_all(
        self, job: ScanJob, params: dict[str, Any],
        refresh: bool, per_strategy: int, persist: bool,
    ) -> dict[str, Any]:
        """逐策略执行，每完成一个就更新进度（供前端进度条使用）。"""
        results: dict[str, Any] = {}
        for key in STRATEGY_ORDER:
            job.current = key

            def on_progress(done: int, total: int, *, _key: str = key) -> None:
                #: 把"单策略内进度"折算进总进度：第 k 个策略跑到 d/t 时，
                #: 总进度 = (已完成策略数 + d/t) / 策略总数。
                frac = (done / total) if total else 0.0
                job.percent_override = round(
                    min(99.0, (job.done + frac) / max(1, job.total) * 100.0), 1,
                )
                job.detail = f"{_key}: 已评估 {done}/{total} 只"

            try:
                outcome = await scan(
                    key, params=params.get(key) or {}, force=refresh,
                    limit=per_strategy, persist=persist, on_progress=on_progress,
                )
                results[key] = outcome.as_dict(limit=per_strategy)
            except Exception as exc:  # noqa: BLE001
                #: 单个策略失败不该让整个任务失败 —— 其余策略照常跑完
                logger.warning("策略 %s 扫描失败: %s", key, exc)
                results[key] = {"strategy": key, "items": [], "error": str(exc)}
            job.done += 1
        job.current = ""
        job.detail = ""
        return {"results": results, "order": list(STRATEGY_ORDER)}

    # ------------------------------------------------------------------ 关闭
    async def shutdown(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        if not tasks:
            return
        logger.info("正在取消 %d 个未完成的扫描任务…", len(tasks))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


scan_jobs = ScanJobService()

__all__ = ["ScanJob", "ScanJobService", "scan_jobs", "TERMINAL"]
