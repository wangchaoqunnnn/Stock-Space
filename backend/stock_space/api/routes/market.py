"""行情路由: 时钟 / 仪表盘 / 指数 / 榜单 / 板块 / 涨停池 / 个股 / 资讯 / 复盘。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ...core.util import normalize_code
from ...engines import review as review_engine
from ...engines.scanner import build_market_context
from ...services import market_service
from ..response import ok

router = APIRouter(tags=["market"])


@router.get("/market/clock")
async def market_clock() -> dict[str, Any]:
    """市场时钟 —— 前端根据 ``should_poll``/``interval_seconds`` 决定轮询节奏。"""
    return ok(market_service.clock())


@router.get("/dashboard")
async def dashboard() -> dict[str, Any]:
    """仪表盘聚合(指数 / 榜单 / 涨跌家数 / 情绪 / 最近扫描)。"""
    return ok(await market_service.dashboard())


@router.get("/market/indices")
async def indices() -> dict[str, Any]:
    return ok(await market_service.indices())


@router.get("/market/breadth")
async def breadth() -> dict[str, Any]:
    return ok((await market_service.breadth()).as_dict())


@router.get("/market/rank")
async def rank(
    kind: str = Query("gainers", pattern="^(gainers|losers|amount|turnover|speed|amplitude|volume_ratio)$"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    return ok(await market_service.rank(kind, limit))


@router.get("/market/sectors")
async def sectors(kind: str = Query("industry", pattern="^(industry|concept)$")) -> dict[str, Any]:
    return ok(await market_service.sectors(kind))


@router.get("/market/sector/{sector_code}")
async def sector_detail(sector_code: str, name: str = "") -> dict[str, Any]:
    return ok(await market_service.sector_detail(sector_code, name))


@router.get("/market/sector-flow")
async def sector_flow(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    return ok(await market_service.sector_flow(limit))


@router.get("/market/limit-up")
async def limit_up() -> dict[str, Any]:
    """涨停/炸板池 —— 含连板梯队与炸板率。"""
    return ok(await market_service.limit_up_pool())


@router.get("/market/emotion")
async def emotion() -> dict[str, Any]:
    """情绪温度计 + 周期阶段 + 龙头分层 + 板块强弱。"""
    return ok(await market_service.emotion_panel())


@router.get("/market/emotion/history")
async def emotion_history(days: int = Query(30, ge=1, le=120)) -> dict[str, Any]:
    return ok(market_service.emotion_history(days))


@router.get("/market/context")
async def market_context(refresh: bool = False) -> dict[str, Any]:
    """当前市场上下文(基准涨幅 / 环境闸门 / 板块统计 / 数据源)。"""
    context = await build_market_context(force=refresh)
    return ok({
        **context.as_dict(),
        "env_gate": context.env_gate,
        "top_sectors": sorted(
            (
                {"name": name, **stats}
                for name, stats in context.sector_stats.items()
            ),
            key=lambda item: -float(item.get("avg_change_pct", 0)),
        )[:20],
    })


@router.get("/quotes")
async def quotes(codes: str = Query(..., description="逗号分隔的证券代码")) -> dict[str, Any]:
    targets = [c.strip() for c in codes.split(",") if c.strip()]
    return ok(await market_service.quotes(targets))


@router.get("/search")
async def search(keyword: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    return ok(await market_service.search(keyword, limit))


@router.get("/stock/{code}")
async def stock_detail(code: str) -> dict[str, Any]:
    return ok(await market_service.stock_detail(normalize_code(code)))


@router.get("/stock/{code}/kline")
async def stock_kline(
    code: str,
    days: int = Query(250, ge=30, le=1200),
    period: str = Query("day", pattern="^(day|week|month)$"),
    indicators: bool = True,
) -> dict[str, Any]:
    return ok(await market_service.kline(normalize_code(code), days, period=period,
                                         with_indicators=indicators))


@router.get("/stock/{code}/minute")
async def stock_minute(code: str) -> dict[str, Any]:
    return ok(await market_service.minute(normalize_code(code)))


@router.get("/news")
async def news(
    limit: int = Query(60, ge=1, le=300),
    channel: str = Query("", pattern="^(|news|flash|announcement|rumor)$"),
) -> dict[str, Any]:
    return ok(await market_service.news(limit=limit, channel=channel))


@router.get("/review")
async def review(kind: str = Query("cn_close", pattern="^(cn_close|us_open)$")) -> dict[str, Any]:
    """行情复盘报告 —— 模块化返回, 每个模块带 status(ok/degraded/missing)。"""
    payload = review_engine.ReviewInput()
    degraded: list[dict[str, str]] = []

    from ...providers.registry import registry as provider_registry

    try:
        quotes = await provider_registry.snapshot()
    except Exception as exc:  # noqa: BLE001
        degraded.append({"block": "quotes", "reason": str(exc)})
        quotes = []
    payload.quotes = quotes
    payload.source = quotes[0].source if quotes else ""

    try:
        payload.breadth = await market_service.breadth()
    except Exception as exc:  # noqa: BLE001
        degraded.append({"block": "breadth", "reason": str(exc)})

    try:
        pool = await market_service.limit_up_pool()
        from ...models import LimitUpStock

        payload.limit_up = [LimitUpStock(**{k: v for k, v in item.items()
                                            if k in LimitUpStock.__dataclass_fields__})
                            for item in pool.get("limit_up", [])]
        payload.broken = [LimitUpStock(**{k: v for k, v in item.items()
                                          if k in LimitUpStock.__dataclass_fields__})
                          for item in pool.get("broken", [])]
    except Exception as exc:  # noqa: BLE001
        degraded.append({"block": "limit_up_pool", "reason": str(exc)})

    try:
        from ...core.util import MAJOR_INDICES
        from ...providers.registry import registry as provider_registry

        benchmark_code = MAJOR_INDICES[0][1]
        kline = await provider_registry.kline(benchmark_code, 120)
        payload.index_kline = kline.closes
        payload.index_quotes = [q for q in quotes if q.code in
                                {code for _, code, _ in MAJOR_INDICES}]
    except Exception as exc:  # noqa: BLE001
        degraded.append({"block": "index_kline", "reason": str(exc)})

    try:
        flow = await market_service.sector_flow(20)
        payload.sector_flow = flow.get("items", [])
    except Exception as exc:  # noqa: BLE001
        degraded.append({"block": "sector_flow", "reason": str(exc)})

    try:
        news_payload = await market_service.news(limit=20)
        payload.news = news_payload.get("items", [])
    except Exception as exc:  # noqa: BLE001
        degraded.append({"block": "news", "reason": str(exc)})

    try:
        emotion_panel = await market_service.emotion_panel()
        payload.emotion = emotion_panel.get("emotion") or {}
        payload.cycle = emotion_panel.get("cycle") or {}
    except Exception as exc:  # noqa: BLE001
        degraded.append({"block": "emotion", "reason": str(exc)})

    report = review_engine.build_report(payload, kind=kind)
    report["degraded_blocks"] = degraded
    return ok(report)


__all__ = ["router"]
