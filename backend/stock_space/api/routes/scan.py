"""异步扫描任务路由：创建任务 + 轮询进度/结果。

设计见 ``services/scan_job_service.py`` 的模块注释 —— 核心动机是**同步长请求**
（单个策略实测 158 秒）会被代理/浏览器空闲超时掐断，前端只能报"无法连接到后端服务"。

    1. ``POST /api/scan/jobs``        创建任务，毫秒级返回 job_id
    2. ``GET  /api/scan/jobs/{id}``   轮询：queued/running/succeeded/failed
    3. ``GET  /api/scan/jobs``        最近任务列表（不含大结果体）

保留原来的同步 ``POST /api/strategies/{key}/scan`` 不删：它是脚本与 curl 的
便捷入口，也是已有测试覆盖的接口。Web 界面走这一套异步接口。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Query

from ...services.scan_job_service import TERMINAL, scan_jobs
from ..response import fail, ok

logger = logging.getLogger(__name__)
router = APIRouter(tags=["scan"])


@router.post("/scan/jobs")
async def create_scan_job(
    strategy: str = Query("all", description="策略 key, 或 all 表示全部策略"),
    refresh: bool = Query(False, description="是否强制刷新上游数据"),
    limit: int = Query(200, ge=1, le=500, description="单策略返回条数上限"),
    per_strategy: int = Query(20, ge=1, le=200, description="all 模式下每策略条数"),
    persist: bool = Query(True, description="是否落库"),
    params: dict | None = Body(None, description="仅 strategy 模式：参数覆盖"),
) -> dict[str, Any]:
    """创建扫描任务并立即返回；扫描在后台执行。"""
    try:
        if strategy == "all":
            job = scan_jobs.start_all(
                refresh=refresh, per_strategy=per_strategy, persist=persist,
            )
        else:
            job = scan_jobs.start_strategy(
                strategy, params=params, refresh=refresh, limit=limit, persist=persist,
            )
    except ValueError as exc:
        return fail(str(exc), code=404, status=404)

    logger.info("已创建扫描任务 %s(kind=%s)", job.id, job.kind)
    return ok({
        "job": job.as_dict(include_result=False),
        "poll": f"api/scan/jobs/{job.id}",
        "hint": "扫描在后台执行；请轮询 poll 字段获取进度与结果。",
    }, "扫描任务已创建")


@router.get("/scan/jobs")
async def list_scan_jobs(limit: int = Query(10, ge=1, le=40)) -> dict[str, Any]:
    """最近的任务列表（不含结果体）。"""
    return ok({"items": scan_jobs.recent(limit), "active": scan_jobs.active()})


@router.get("/scan/jobs/{job_id}")
async def scan_job_status(job_id: str, result: bool = Query(True)) -> dict[str, Any]:
    """查询任务状态；``status`` 进入 succeeded/failed 后即为终态。

    未完成时 ``result`` 为 None，前端可据 ``percent``/``done``/``total`` 画进度条。
    """
    job = scan_jobs.get(job_id)
    if job is None:
        return fail(f"任务不存在或已过期: {job_id}", code=404, status=404)
    payload = job.as_dict(include_result=result)
    payload["terminal"] = job.status in TERMINAL
    return ok(payload)


@router.post("/scan/jobs/{job_id}/cancel")
async def cancel_scan_job(job_id: str) -> dict[str, Any]:
    """请求取消任务（若已进入终态则原样返回）。"""
    job = scan_jobs.get(job_id)
    if job is None:
        return fail(f"任务不存在或已过期: {job_id}", code=404, status=404)
    if job.status in TERMINAL:
        return ok(job.as_dict(include_result=False), "任务已结束，无需取消")
    from ...services.scan_job_service import scan_jobs as _jobs

    task = _jobs._tasks.get(job_id)  # noqa: SLF001
    if task is not None and not task.done():
        task.cancel()
        return ok(job.as_dict(include_result=False), "已请求取消")
    return ok(job.as_dict(include_result=False), "任务未在运行")
