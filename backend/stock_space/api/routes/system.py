"""健康检查与系统信息路由(无需鉴权)。"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter

from ... import __version__
from ...config import config
from ...core.memory import memory_guard, read_rss_mb, read_system_memory
from ...core.util import market_session, now_cn, today_str
from ...providers.registry import registry
from ...services import scheduler
from ...services.user_service import INTEGRATION_MAP, system_overview
from ..response import ok

router = APIRouter(tags=["system"])

_START_TS = time.time()


@router.get("/health")
async def health() -> dict[str, Any]:
    """存活检查 —— 云监控直接打这个接口即可。

    ``degraded`` 字段非空表示有降级项, 但服务本身仍在运行(不返回 5xx),
    这样监控可以区分"进程死了"和"某个数据源挂了"。
    """
    session = market_session()
    degraded: list[str] = []
    try:
        sources = registry.report()
        categories = sources.get("categories", {})
        empty = [cap for cap, info in categories.items() if not info.get("effective_order")]
        if empty:
            degraded.append(f"{len(empty)} 个数据能力无可用源: {', '.join(sorted(empty)[:5])}")
        available = len(categories) - len(empty)
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"数据源状态不可读: {exc}")
        available = total = 0
    else:
        total = len(categories)
    if not scheduler.running:
        degraded.append("调度器未运行")

    memory = memory_guard.report(trend_points=5)
    limits = memory.get("limits", {})
    if limits.get("hard_usage_pct", 0) >= 90:
        degraded.append("内存占用接近硬上限")
    elif limits.get("soft_usage_pct", 0) >= 100:
        degraded.append("内存占用超过软上限")

    return ok({
        "status": "ok" if not degraded else "degraded",
        "degraded": degraded,
        "version": __version__,
        "uptime_seconds": round(time.time() - _START_TS, 1),
        "pid": os.getpid(),
        "rss_mb": round(read_rss_mb(), 2),
        "trade_date": today_str(),
        "server_time": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "session": session.as_dict(),
        "source_mode": config().source_mode,
        "scheduler_running": scheduler.running,
        "capabilities_available": available,
        "capabilities_total": total,
    })


@router.get("/ready")
async def ready() -> dict[str, Any]:
    """就绪检查 —— 容器编排的 readinessProbe 用。"""
    issues: list[str] = []
    if not registry.all():
        issues.append("尚无已注册的数据源")
    try:
        from ...store.db import db

        db.query_one("SELECT 1 AS ok")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"数据库不可用: {exc}")
    return ok({"ready": not issues, "issues": issues})


@router.get("/version")
async def version() -> dict[str, Any]:
    return ok({"version": __version__, "title": config().get("app.title")})


@router.get("/system/overview")
async def overview() -> dict[str, Any]:
    """系统总览: 应用信息 + 内存 + 调度 + 数据库 + 数据源统计。"""
    return ok(system_overview(
        memory_report=memory_guard.report(),
        scheduler_report=scheduler.report(),
    ))


@router.get("/system/memory")
async def memory(points: int = 120) -> dict[str, Any]:
    """内存监控数据(需求: 需要能监控内存情况)。"""
    return ok(memory_guard.report(trend_points=max(10, min(720, points))))


@router.post("/system/memory/flush")
async def memory_flush() -> dict[str, Any]:
    """立即释放全部缓存并触发 gc。"""
    return ok(memory_guard.flush(), "缓存已释放")


@router.get("/system/memory/raw")
async def memory_raw() -> dict[str, Any]:
    """极简内存视图(适合脚本/监控系统解析)。"""
    system = read_system_memory()
    return ok({
        "rss_mb": round(read_rss_mb(), 2),
        "system_used_pct": system.get("used_pct"),
        "system_total_mb": system.get("total_mb"),
        "cache_entries": memory_guard.total_entries,
        "soft_breaches": memory_guard.state.soft_breaches,
        "hard_breaches": memory_guard.state.hard_breaches,
    })


@router.get("/system/scheduler")
async def scheduler_status() -> dict[str, Any]:
    return ok(scheduler.report())


@router.get("/system/jobs")
async def jobs(limit: int = 50) -> dict[str, Any]:
    return ok({"items": scheduler.recent_jobs(limit)})


@router.post("/system/jobs/{job}/run")
async def run_job(job: str) -> dict[str, Any]:
    """手动触发一个定时任务(便于部署后立即验证)。"""
    try:
        return ok(await scheduler.run_once(job))
    except ValueError as exc:
        from ..response import fail

        return fail(str(exc), code=400, status=400)


@router.get("/system/database")
async def database() -> dict[str, Any]:
    from ...store.db import db

    return ok(db.stats())


@router.post("/system/database/cleanup")
async def database_cleanup() -> dict[str, Any]:
    from ...services.market_service import cleanup

    return ok(cleanup(), "清理完成")


@router.post("/system/database/vacuum")
async def database_vacuum() -> dict[str, Any]:
    from ...store.db import db

    db.vacuum()
    return ok(db.stats(), "VACUUM 完成")


@router.get("/system/integration")
async def integration() -> dict[str, Any]:
    """原始项目 → 本平台的落地对照表(需求文档要求可追溯)。"""
    return ok({"items": INTEGRATION_MAP})
