"""策略路由: 目录 / 扫描 / 单只评估 / 回测 / 信号流水。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Query

from ...config import config
from ...core.util import normalize_code
from ...engines import STRATEGY_ORDER, all_strategies, catalog, get as get_strategy
from ...engines.backtest import Backtester
from ...engines.base import build_series
from ...engines.scanner import (
    build_market_context,
    evaluate_one,
    last_scan,
    scan,
    scan_many,
)
from ...models import KLine, Quote
from ...providers.registry import registry
from ...services import user_service
from ...store.settings_store import settings_store
from ...store.kline_store import kline_store
from ..response import fail, ok

logger = logging.getLogger(__name__)
router = APIRouter(tags=["strategy"])


@router.get("/strategies")
async def strategies() -> dict[str, Any]:
    """策略目录: 元信息 + 默认参数 + 逐条规则 + 回测口径。"""
    params = settings_store.all_strategy_params()
    items = []
    for meta in catalog():
        merged = dict(meta)
        merged["params"] = {**meta.get("params", {}), **(params.get(meta["key"]) or {})}
        merged["user_overrides"] = params.get(meta["key"]) or {}
        items.append(merged)
    return ok({"items": items, "order": list(STRATEGY_ORDER)})


@router.get("/strategies/{key}")
async def strategy_detail(key: str) -> dict[str, Any]:
    strategy = get_strategy(key)
    if strategy is None:
        return fail(f"未知策略: {key}", code=404, status=404)
    overrides = settings_store.all_strategy_params().get(key) or {}
    doc = strategy.doc()
    doc["params"] = strategy.merged_params(overrides)
    doc["user_overrides"] = overrides
    return ok(doc)


@router.post("/strategies/{key}/params")
async def save_strategy_params(key: str, payload: dict = Body(...)) -> dict[str, Any]:
    """保存用户参数覆盖(只允许该策略声明的键)。"""
    strategy = get_strategy(key)
    if strategy is None:
        return fail(f"未知策略: {key}", code=404, status=404)
    cleaned: dict[str, Any] = {}
    rejected: list[str] = []
    defaults = strategy.PARAMS
    for name, value in payload.items():
        if name not in defaults:
            rejected.append(name)
            continue
        try:
            cleaned[name] = type(defaults[name])(value)
        except (TypeError, ValueError):
            rejected.append(name)
    settings_store.save_strategy_params(key, cleaned)
    return ok({
        "strategy": key,
        "params": strategy.merged_params(cleaned),
        "saved": sorted(cleaned.keys()),
        "rejected": rejected,
    }, "参数已保存")


@router.delete("/strategies/{key}/params")
async def reset_strategy_params(key: str) -> dict[str, Any]:
    strategy = get_strategy(key)
    if strategy is None:
        return fail(f"未知策略: {key}", code=404, status=404)
    settings_store.save_strategy_params(key, {})
    return ok({"strategy": key, "params": strategy.PARAMS}, "已恢复默认参数")


@router.post("/strategies/{key}/scan")
async def run_scan(
    key: str,
    refresh: bool = Query(False, description="是否强制刷新上游数据"),
    limit: int = Query(100, ge=1, le=500),
    persist: bool = Query(True, description="是否落库"),
    params: dict | None = Body(None),
) -> dict[str, Any]:
    if get_strategy(key) is None:
        return fail(f"未知策略: {key}", code=404, status=404)
    merged = {**(settings_store.all_strategy_params().get(key) or {}), **(params or {})}
    try:
        outcome = await scan(key, params=merged, force=refresh, limit=limit, persist=persist)
    except RuntimeError as exc:
        return fail(str(exc), code=503, status=503)
    payload = outcome.as_dict(limit=limit)
    if persist and outcome.signals:
        try:
            await asyncio.to_thread(user_service.log_signals, outcome.signals, strategy=key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("信号流水写入失败: %s", exc)
    return ok(payload)


@router.post("/scan/all")
async def run_scan_all(
    refresh: bool = Query(False),
    per_strategy: int = Query(20, ge=1, le=200),
    persist: bool = Query(True),
) -> dict[str, Any]:
    """一次跑完全部策略(共享同一份市场上下文与日线缓存)。"""
    params = settings_store.all_strategy_params()
    try:
        outcomes = await scan_many(
            STRATEGY_ORDER, params_by_strategy=params, force=refresh,
            per_strategy_limit=per_strategy,
        )
    except RuntimeError as exc:
        return fail(str(exc), code=503, status=503)
    return ok({
        "results": {key: outcome.as_dict(limit=per_strategy) for key, outcome in outcomes.items()},
        "order": list(STRATEGY_ORDER),
    })


@router.get("/strategies/{key}/last")
async def last_result(key: str, date: str = "") -> dict[str, Any]:
    result = last_scan(key, date or None)
    if result is None:
        return ok({"strategy": key, "items": [], "hint": "尚无历史扫描结果, 请先执行一次扫描"})
    return ok(result)


@router.post("/strategies/{key}/evaluate")
async def evaluate_stock(
    key: str,
    code: str = Body(..., embed=True),
    params: dict | None = Body(None, embed=True),
) -> dict[str, Any]:
    """对单只股票跑一个策略 —— 用于个股页的"为什么入选/未入选"。"""
    strategy = get_strategy(key)
    if strategy is None:
        return fail(f"未知策略: {key}", code=404, status=404)
    code = normalize_code(code)
    context = await build_market_context()
    quote = next((q for q in context.quotes if q.code == code), None)
    if quote is None:
        try:
            fetched = await registry.quotes([code])
            quote = fetched[0] if fetched else None
        except Exception as exc:  # noqa: BLE001
            return fail(f"无法获取 {code} 的行情: {exc}", code=503, status=503)
    if quote is None:
        return fail(f"未找到证券 {code}", code=404, status=404)

    kline = kline_store.get(code, 260)
    if kline is None or len(kline.bars) < strategy.min_bars:
        try:
            kline = await registry.kline(code, 260)
        except Exception as exc:  # noqa: BLE001
            return fail(f"无法获取 {code} 的日线: {exc}", code=503, status=503)
        if kline is not None:
            kline_store.put(kline, source=kline.source)
    if kline is None or len(kline.bars) < 5:
        return fail(f"{code} 的日线数据不足, 无法评估", code=422, status=422)

    merged = {**(settings_store.all_strategy_params().get(key) or {}), **(params or {})}
    signal = await asyncio.to_thread(evaluate_one, strategy, quote, kline, merged, context)
    return ok({**signal.as_dict(), "context": context.as_dict()})


@router.post("/backtest")
async def run_backtest(payload: dict = Body(...)) -> dict[str, Any]:
    """回测。

    请求体::

        {
          "strategy": "trend",
          "params": {...},              # 可选, 覆盖策略参数
          "codes": ["600519", ...],     # 可选, 限定标的; 缺省则用全市场(受配额限制)
          "days": 250,                  # 回看天数
          "fill": "next_open",          # next_open(默认) | close
          "max_positions": 5,
          "position_pct": 0.2,
          "limit": 200                  # 标的数量上限
        }
    """
    key = str(payload.get("strategy") or "trend")
    strategy = get_strategy(key)
    if strategy is None:
        return fail(f"未知策略: {key}", code=404, status=404)

    days = int(payload.get("days") or 250)
    days = max(120, min(800, days))
    limit = int(payload.get("limit") or 200)
    limit = max(1, min(800, limit))
    fill = str(payload.get("fill") or "next_open")

    codes = payload.get("codes") or []
    if codes:
        targets = [normalize_code(c) for c in codes]
    else:
        context = await build_market_context()
        snapshot = sorted(context.quotes, key=lambda q: -q.amount)[:limit]
        targets = [q.code for q in snapshot]
    if not targets:
        return fail("没有可用于回测的标的", code=422, status=422)

    names = {q.code: q for q in (await _all_quotes())}
    universe: list[tuple[Quote, KLine]] = []
    failures = 0
    semaphore = asyncio.Semaphore(int(config().get("quotas.kline_concurrency", 16)))

    async def fetch(code: str) -> tuple[Quote, KLine] | None:
        nonlocal failures
        async with semaphore:
            kline = kline_store.get(code, days)
            if kline is None or len(kline.bars) < days * 0.5:
                try:
                    kline = await registry.kline(code, days)
                except Exception:  # noqa: BLE001
                    failures += 1
                    return None
                if kline is not None:
                    kline_store.put(kline, source=kline.source)
            if kline is None or len(kline.bars) < 100:
                failures += 1
                return None
        quote = names.get(code) or Quote(code=code)
        return quote, kline

    results = await asyncio.gather(*(fetch(code) for code in targets))
    universe = [item for item in results if item is not None]
    if not universe:
        return fail("没有取到足够的日线数据, 无法回测(请稍后重试或减少标的数量)", code=503, status=503)

    params = {**(settings_store.all_strategy_params().get(key) or {}), **(payload.get("params") or {})}
    backtester = Backtester(
        strategy,
        params,
        fill=fill,
        max_positions=int(payload.get("max_positions") or 5),
        position_pct=float(payload.get("position_pct") or 0.2),
        min_score=float(payload["min_score"]) if payload.get("min_score") is not None else None,
    )
    result = await asyncio.to_thread(backtester.run, universe)
    data = result.as_dict()
    data["requested"] = len(targets)
    data["available"] = len(universe)
    data["failed"] = failures
    return ok(data)


async def _all_quotes() -> list[Quote]:
    try:
        return await registry.snapshot()
    except Exception:  # noqa: BLE001
        return []


@router.get("/signals")
async def signals(strategy: str = "", limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    from ...services.user_service import list_signals

    return ok({"items": list_signals(strategy=strategy, limit=limit)})


__all__ = ["router"]
