"""定时任务调度器。

一个独立的后台 asyncio 任务, 按"交易时段/非交易时段"两档节奏运行:

  * **交易时段**(09:15~15:00 的工作日, 间隔 ``trading_interval_seconds``, 默认 30s):
    刷新全市场快照 → 检查是否触发策略信号/数据源异常/内存告警 → 按需推送;
  * **非交易时段**(间隔 ``idle_interval_seconds``, 默认 300s): 轻量心跳 + 自动清理;
  * **每日任务**(默认 15:10): 全策略扫描落库 + 资讯保留期清理 + 数据源健康归档;
  * **定时推送**(``push.schedules``, 默认 09:00 / 11:35 / 15:05): 推送市场情绪摘要。

任务失败**绝不抛出到外层**: 每个任务独立 try/except 并写入 ``job_log``,
单个任务坏掉不影响其它任务 —— 这是"云端 7×24 小时运行"的基本要求。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import config
from ..core.memory import memory_guard
from ..core.util import CN_TZ, market_session, now_cn, today_str
from ..providers.registry import AllProvidersFailed, registry
from ..store.db import db
from ..store.settings_store import settings_store
from . import market_service, push_service
from .push_service import pusher

logger = logging.getLogger(__name__)


@dataclass
class JobStat:
    name: str
    runs: int = 0
    failures: int = 0
    last_run_ts: float = 0.0
    last_duration_ms: float = 0.0
    last_error: str = ""
    last_detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "runs": self.runs,
            "failures": self.failures,
            "last_run_ago_seconds": round(time.time() - self.last_run_ts, 1) if self.last_run_ts else None,
            "last_duration_ms": round(self.last_duration_ms, 1),
            "last_error": self.last_error,
            "last_detail": self.last_detail,
        }


class Scheduler:
    """后台任务调度器。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.stats: dict[str, JobStat] = {}
        self.started_at: float = 0.0
        self.loop_count = 0
        self.last_tick_ts: float = 0.0
        self._done_daily: set[str] = set()
        self._pushed_schedule: set[str] = set()
        self._last_signals: dict[str, set[str]] = {}
        self._source_alert_sent: float = 0.0
        self._memory_alert_sent: float = 0.0
        self._warned_capabilities: set[str] = set()

    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="stock-space-scheduler")
        logger.info("调度器已启动")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        logger.info("调度器已停止")

    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        # 启动后先等一个短周期, 让应用完成初始化
        await asyncio.sleep(3)
        while not self._stop.is_set():
            interval = 300.0
            try:
                session = market_session()
                interval = float(
                    config().get("scheduler.trading_interval_seconds", 30)
                    if session.is_trading else
                    config().get("scheduler.idle_interval_seconds", 300)
                )
                if not config().get("scheduler.enabled", True):
                    interval = max(interval, 300.0)
                    await self._sleep(interval)
                    continue
                await self._tick(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 调度循环绝不允许退出
                logger.exception("调度循环异常(已忽略)")
                self._record("tick", error=str(exc))
            await self._sleep(max(5.0, interval))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ #
    async def _tick(self, session) -> None:
        self.loop_count += 1
        self.last_tick_ts = time.time()

        if session.should_poll or session.is_trading:
            await self._job("refresh_snapshot", self._refresh_snapshot)
            await self._job("signal_watch", self._signal_watch)

        await self._job("scheduled_push", self._scheduled_push)
        await self._job("daily_tasks", self._daily_tasks)
        await self._job("memory_alert", self._memory_alert)
        await self._job("housekeeping", self._housekeeping)

    # ------------------------------------------------------------------ #
    async def _refresh_snapshot(self) -> str:
        try:
            quotes = await registry.snapshot(force=True)
        except AllProvidersFailed as exc:
            await self._notify_source_failure("snapshot", exc.errors)
            raise RuntimeError(exc.detail) from exc
        return f"刷新 {len(quotes)} 只标的"

    async def _signal_watch(self) -> str:
        """持续扫描"当前处于交易时段"的轻量信号, 只推送**新增**标的。"""
        if not config().get("push.enabled", False):
            return "推送未开启, 跳过"
        if not config().get("push.events.strategy_signal", True):
            return "策略信号推送已关闭"

        from ..engines import STRATEGY_ORDER, all_strategies, scan
        from ..engines.scanner import build_market_context

        context = await build_market_context()
        min_score = float(config().get("push.min_signal_score", 75.0) or 75.0)
        pushed_keys = [k for k in (config().get("push.signal_strategies", []) or [])]
        keys = pushed_keys or ["quiet_rise", "limit_up_pullback", "trend"]
        keys = [k for k in keys if k in STRATEGY_ORDER]
        names = {s.key: s.name for s in all_strategies()}

        summary: list[str] = []
        for key in keys:
            try:
                outcome = await scan(key, force=False, limit=20, context=context, persist=False)
            except Exception as exc:  # noqa: BLE001
                summary.append(f"{key}: 扫描失败")
                continue
            fresh = [
                s for s in outcome.signals
                if s.passed and s.score >= min_score
            ][:10]
            current = {s.code for s in fresh}
            previous = self._last_signals.get(key, set())
            new_codes = current - previous
            self._last_signals[key] = current
            if not new_codes:
                continue
            items = [s.as_dict() for s in fresh if s.code in new_codes]
            message = push_service.signal_message(
                strategy_name=names.get(key, key), items=items,
                base_url=settings_store.get("app.public_base_url", ""),
                limit=10,
            )
            result = await pusher.send(message)
            summary.append(f"{key}: 新增 {len(items)} 只, 推送{'成功' if result.ok else '失败'}")

        return "; ".join(summary) if summary else "无新增信号"

    async def _scheduled_push(self) -> str:
        """按 ``push.schedules`` 在固定时刻推送市场情绪摘要。"""
        if not pusher.enabled:
            return "推送未配置"
        raw = str(config().get("push.schedules", "") or "")
        slots = [s.strip() for s in raw.split(",") if s.strip()]
        now = now_cn()
        hhmm = now.strftime("%H:%M")
        date_key = now.strftime("%Y-%m-%d")
        if hhmm not in slots:
            return "非推送时刻"
        marker = f"{date_key} {hhmm}"
        if marker in self._pushed_schedule:
            return "本时段已推送"
        self._pushed_schedule.add(marker)
        if len(self._pushed_schedule) > 64:
            self._pushed_schedule = {m for m in self._pushed_schedule if m.startswith(date_key)}

        panel = await market_service.emotion_panel()
        emotion = panel.get("emotion") or {}
        cycle = panel.get("cycle") or {}
        breadth = None
        try:
            breadth = (await market_service.breadth()).as_dict()
        except Exception:  # noqa: BLE001
            pass
        message = push_service.market_message(
            emotion=emotion, cycle=cycle, breadth=breadth,
            base_url=settings_store.get("app.public_base_url", ""),
        )
        result = await pusher.send(message)
        return f"{hhmm} 推送{'成功' if result.ok else '失败: ' + result.error}"

    async def _daily_tasks(self) -> str:
        """每个交易日收盘后执行一次: 全策略扫描 + 清理。"""
        now = now_cn()
        hour = int(config().get("scheduler.daily_job_hour", 15) or 15)
        minute = int(config().get("scheduler.daily_job_minute", 10) or 10)
        date_key = f"{today_str()}"
        if date_key in self._done_daily:
            return "今日已完成"
        session = market_session()
        if not session.is_trading_day:
            self._done_daily.add(date_key)
            return "非交易日, 跳过"
        if (now.hour, now.minute) < (hour, minute):
            return "未到执行时刻"

        self._done_daily.add(date_key)
        results: list[str] = []
        try:
            from ..engines import STRATEGY_ORDER, scan_many

            outcomes = await scan_many(STRATEGY_ORDER, per_strategy_limit=50)
            for key, outcome in outcomes.items():
                results.append(f"{key}={outcome.passed_count}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"扫描失败: {exc}")
        try:
            cleaned = market_service.cleanup()
            results.append(f"清理 {cleaned}")
        except Exception as exc:  # noqa: BLE001
            results.append(f"清理失败: {exc}")
        return "; ".join(results)

    async def _memory_alert(self) -> str:
        if not config().get("push.events.memory_warning", True):
            return "内存告警已关闭"
        state = memory_guard.state
        if state.soft_breaches == 0 and state.hard_breaches == 0:
            return "内存正常"
        now = time.time()
        if now - self._memory_alert_sent < 3600:
            return "1 小时内已告警"
        self._memory_alert_sent = now
        report = memory_guard.report(trend_points=10)
        if not pusher.enabled:
            logger.warning("内存触发上限但未配置推送: %s", memory_guard.log_line())
            return "触发上限(未配置推送)"
        message = push_service.memory_alert_message(
            report=report, base_url=settings_store.get("app.public_base_url", "")
        )
        result = await pusher.send(message)
        return f"已告警 ({'成功' if result.ok else '失败'})"

    async def _housekeeping(self) -> str:
        """轻量维护: 内存日志行 + 数据源健康归档。"""
        memory_guard.log_line()
        try:
            _record_source_health()
        except Exception:  # noqa: BLE001
            pass
        return "ok"

    # ------------------------------------------------------------------ #
    async def _notify_source_failure(self, capability: str, errors: list[tuple[str, str]]) -> None:
        if not config().get("push.events.source_down", True):
            return
        if capability in self._warned_capabilities:
            return
        self._warned_capabilities.add(capability)
        if pusher.enabled:
            message = push_service.source_alert_message(
                capability=capability, errors=errors,
                base_url=settings_store.get("app.public_base_url", ""),
            )
            await pusher.send(message)

    # ------------------------------------------------------------------ #
    async def _job(self, name: str, func: Callable[[], Awaitable[str]]) -> None:
        started = time.perf_counter()
        try:
            detail = await func()
            self._record(name, detail=detail, latency_ms=(time.perf_counter() - started) * 1000.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单任务失败不影响其它任务
            logger.warning("任务 %s 失败: %s", name, exc)
            self._record(name, error=str(exc), latency_ms=(time.perf_counter() - started) * 1000.0)

    def _record(self, name: str, *, error: str = "", detail: str = "", latency_ms: float = 0.0) -> None:
        stat = self.stats.setdefault(name, JobStat(name=name))
        stat.runs += 1
        stat.last_run_ts = time.time()
        stat.last_duration_ms = latency_ms
        if error:
            stat.failures += 1
            stat.last_error = error[:300]
        else:
            stat.last_error = ""
        stat.last_detail = (detail or "")[:300]
        try:
            db.execute(
                "INSERT INTO job_log(job, status, detail, duration_ms, created_at) VALUES(?,?,?,?,?)",
                (name, "error" if error else "ok", (error or detail)[:1000], latency_ms, time.time()),
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    async def run_once(self, job: str) -> dict[str, Any]:
        """手动触发某个任务(「系统」页面的按钮)。"""
        mapping: dict[str, Callable[[], Awaitable[str]]] = {
            "refresh_snapshot": self._refresh_snapshot,
            "signal_watch": self._signal_watch,
            "scheduled_push": self._scheduled_push,
            "daily_tasks": self._daily_tasks,
            "housekeeping": self._housekeeping,
            "memory_alert": self._memory_alert,
        }
        func = mapping.get(job)
        if func is None:
            raise ValueError(f"未知任务: {job}")
        started = time.perf_counter()
        try:
            detail = await func()
            self._record(job, detail=detail, latency_ms=(time.perf_counter() - started) * 1000.0)
            return {"job": job, "ok": True, "detail": detail,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 1)}
        except Exception as exc:  # noqa: BLE001
            self._record(job, error=str(exc), latency_ms=(time.perf_counter() - started) * 1000.0)
            return {"job": job, "ok": False, "error": str(exc),
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 1)}

    def report(self) -> dict[str, Any]:
        session = market_session()
        return {
            "running": self.running,
            "enabled": bool(config().get("scheduler.enabled", True)),
            "started_at": self.started_at,
            "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "loop_count": self.loop_count,
            "last_tick_ago_seconds": round(time.time() - self.last_tick_ts, 1) if self.last_tick_ts else None,
            "session": session.as_dict(),
            "interval_seconds": config().get(
                "scheduler.trading_interval_seconds" if session.is_trading
                else "scheduler.idle_interval_seconds"
            ),
            "jobs": [stat.as_dict() for stat in self.stats.values()],
            "daily_done": sorted(self._done_daily),
            "push_schedules": str(config().get("push.schedules", "") or ""),
            "pushed_today": sorted(m for m in self._pushed_schedule if m.startswith(today_str())),
        }

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = db.query(
            "SELECT * FROM job_log ORDER BY id DESC LIMIT ?", (max(1, min(200, limit)),)
        )
        return [
            {
                "job": row["job"], "status": row["status"], "detail": row["detail"],
                "duration_ms": round(row["duration_ms"], 1),
                "created_at": row["created_at"],
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"])),
            }
            for row in rows
        ]


def _record_source_health() -> None:
    """把当前各源健康度落库(供趋势图), 只保留最近 7 天。"""
    from ..core.http import metrics_board

    rows = []
    now = time.time()
    for metric in metrics_board.all():
        if metric.attempts == 0:
            continue
        rows.append((metric.alias, "aggregate", 1 if metric.success_rate >= 0.5 else 0,
                     metric.avg_latency_ms, metric.last_error[:200], now))
    if not rows:
        return
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO source_health(alias, capability, ok, latency_ms, error, created_at) "
            "VALUES(?,?,?,?,?,?)",
            rows,
        )


#: 全局单例
scheduler = Scheduler()


__all__ = ["Scheduler", "scheduler", "JobStat"]
