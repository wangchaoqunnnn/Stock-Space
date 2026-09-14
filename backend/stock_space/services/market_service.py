"""市场数据服务: 行情/榜单/板块/情绪/资讯 的高层聚合。

API 层只调用本模块, 不直接接触 provider 或引擎 —— 保证"换源"与"改策略"
都不会波及接口契约。

设计要点:
  * 所有返回都带 ``source``(实际生效的数据源)与 ``as_of``(数据时间), 前端必须能显示"数据来自哪里";
  * 小内存机器通过 ``universe_size`` 限制样本时, 返回 ``sample_limited=true`` 明确标注;
  * 上游不可用时抛 ``ServiceUnavailable``, 由 API 层统一转成 503 + 可读原因,
    **绝不返回编造的占位数据**。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Iterable, Sequence

import numpy as np

from ..config import config
from ..core.util import (
    CN_TZ,
    MAJOR_INDICES,
    limit_pct,
    market_session,
    now_cn,
    normalize_code,
    today_str,
)
from ..models import Bar, Breadth, KLine, LimitUpStock, Quote
from ..providers.registry import AllProvidersFailed, registry
from ..store.kline_store import kline_store
from ..engines import emotion as emotion_engine
from ..engines.indicators import tag_indicators

logger = logging.getLogger(__name__)


class ServiceUnavailable(RuntimeError):
    """上游数据不可用 —— API 层转 503。"""

    def __init__(self, capability: str, detail: str = "") -> None:
        super().__init__(detail or f"数据能力 [{capability}] 当前不可用")
        self.capability = capability
        self.detail = detail


def _unavailable(capability: str, exc: Exception) -> ServiceUnavailable:
    if isinstance(exc, AllProvidersFailed):
        return ServiceUnavailable(capability, exc.detail)
    return ServiceUnavailable(capability, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 市场时钟 / 概览
# --------------------------------------------------------------------------- #
def clock() -> dict[str, Any]:
    """市场时钟 —— 前端据此决定轮询节奏。"""
    session = market_session()
    data = session.as_dict()
    data.update({
        "server_time": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "UTC+8",
        "refresh_hint_seconds": session.interval_seconds,
    })
    return data


async def indices() -> dict[str, Any]:
    """主要指数行情。

    **不再用全市场快照去匹配指数**: 指数与个股代码段重叠(``000001`` 既是上证指数
    也是平安银行, ``399001`` 是深证成指), 用股票快照匹配会把平安银行当成上证指数
    显示 —— 这是实测踩到的真实 bug。改为调用专门的指数接口, 并逐个校验点位。
    """
    specs = [(name, code) for name, code, _ in MAJOR_INDICES]
    items: list[dict[str, Any]] = []
    source = ""
    errors: list[str] = []

    try:
        outcome = await registry.call("indices", specs)
        quotes = outcome.value if isinstance(outcome.value, list) else []
        source = outcome.alias
        by_code = {q.code: q for q in quotes}
        for name, code, market in MAJOR_INDICES:
            match = by_code.get(code)
            if match is None:
                items.append({"name": name, "code": code, "market": market, "price": 0.0,
                              "change": 0.0, "change_pct": 0.0, "amount": 0.0,
                              "source": "", "missing": True})
            else:
                data = match.as_dict()
                data["market"] = market
                items.append(data)
    except Exception as exc:  # noqa: BLE001 - 指数不可用不应让整个页面报错
        errors.append(f"{type(exc).__name__}: {exc}")
        for name, code, market in MAJOR_INDICES:
            items.append({"name": name, "code": code, "market": market, "price": 0.0,
                          "change": 0.0, "change_pct": 0.0, "amount": 0.0,
                          "source": "", "missing": True})

    return {
        "items": items,
        "matched": sum(1 for item in items if not item.get("missing")),
        "total": len(items),
        "source": source,
        "errors": errors,
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def breadth(*, quotes: Sequence[Quote] | None = None) -> Breadth:
    """市场涨跌家数。

    **优先复用调用方手里已有的全市场快照** —— 仪表盘/复盘页都已经拉过快照了，
    再去打一次上游要 7~8 秒（实测），纯属浪费。只有确实拿不到快照时才请求上游。
    """
    if quotes:
        return derive_breadth(quotes, source="snapshot_derived")

    cached = registry.cached_snapshot()
    if cached:
        return derive_breadth(cached, source="snapshot_derived")

    try:
        return await registry.breadth()
    except Exception:  # noqa: BLE001 - 退回快照统计
        try:
            quotes = await registry.snapshot()
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("breadth", exc) from exc
        return derive_breadth(quotes, source="snapshot_derived")


def derive_breadth(quotes: Sequence[Quote], *, source: str = "snapshot_derived") -> Breadth:
    """由快照统计涨跌家数（样本口径，但在全市场覆盖时即为全市场口径）。"""
    up = down = flat = 0
    limit_up = limit_down = 0
    up5 = down5 = 0
    amount = 0.0
    for quote in quotes:
        amount += quote.amount
        if quote.change_pct > 0:
            up += 1
        elif quote.change_pct < 0:
            down += 1
        else:
            flat += 1
        threshold = limit_pct(quote.code, quote.name)
        if quote.change_pct >= threshold - 0.3:
            limit_up += 1
        elif quote.change_pct <= -(threshold - 0.3):
            limit_down += 1
        if quote.change_pct >= 5:
            up5 += 1
        elif quote.change_pct <= -5:
            down5 += 1
    return Breadth(
        up=up, down=down, flat=max(0, flat), limit_up=limit_up, limit_down=limit_down,
        up_over_5=up5, down_over_5=down5, total_amount=amount, source=source,
    )


async def dashboard() -> dict[str, Any]:
    """仪表盘聚合。任何单块失败都降级为 ``null`` 并记录原因, 不整体报错。"""
    session = market_session()
    payload: dict[str, Any] = {
        "clock": session.as_dict(),
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": today_str(),
        "degraded": [],
    }

    # 只并发两路：快照 + 涨停池。
    # 涨跌家数不单独发请求 —— 它可以从快照本地统计（有专用接口时才用，见 _breadth_from_quotes_or_api）。
    snapshot_task = asyncio.create_task(registry.snapshot())
    pool_task = asyncio.create_task(_safe(registry.limit_up_pool()))

    try:
        quotes = await snapshot_task
    except Exception as exc:  # noqa: BLE001
        payload["degraded"].append({"block": "quotes", "reason": str(exc)})
        quotes = []

    try:
        payload["breadth"] = (await breadth(quotes=quotes)).as_dict()
    except Exception as exc:  # noqa: BLE001
        payload["degraded"].append({"block": "breadth", "reason": str(exc)})
        payload["breadth"] = None

    pool = await pool_task
    limit_up_list: list[LimitUpStock] = []
    broken_list: list[LimitUpStock] = []
    if isinstance(pool, dict):
        limit_up_list = pool.get("limit_up") or []
        broken_list = pool.get("broken") or []
    else:
        payload["degraded"].append({"block": "limit_up_pool", "reason": "涨停池接口不可用"})

    payload["sample_limited"] = bool(config().universe_size)
    payload["universe_size"] = len(quotes)

    limit_n = config().universe_size
    sample = quotes if not limit_n else sorted(quotes, key=lambda q: -q.amount)[:limit_n]

    if sample:
        advancers = sorted([q for q in sample if q.change_pct > 0], key=lambda q: -q.change_pct)[:10]
        decliners = sorted([q for q in sample if q.change_pct < 0], key=lambda q: q.change_pct)[:10]
        by_amount = sorted(sample, key=lambda q: -q.amount)[:10]
        by_turnover = sorted(sample, key=lambda q: -q.turnover_rate)[:10]
        payload["leaders"] = {
            "gainers": [q.as_dict() for q in advancers],
            "losers": [q.as_dict() for q in decliners],
            "amount": [q.as_dict() for q in by_amount],
            "turnover": [q.as_dict() for q in by_turnover],
        }
    else:
        payload["leaders"] = {"gainers": [], "losers": [], "amount": [], "turnover": []}

    # 指数走独立接口 —— 指数与个股代码段重叠(000001 既是上证指数也是平安银行),
    # 用股票快照匹配会把个股当指数显示。
    try:
        index_payload = await indices()
        payload["index_quotes"] = [
            item for item in index_payload["items"] if not item.get("missing")
        ]
        payload["indices_degraded"] = index_payload.get("errors") or []
    except Exception as exc:  # noqa: BLE001
        payload["index_quotes"] = []
        payload["degraded"].append({"block": "indices", "reason": str(exc)})

    # 情绪: 用已拿到的快照与涨停池, 不再重复请求
    if sample:
        try:
            previous = _load_previous_emotion()
            payload["emotion"] = emotion_engine.snapshot(
                quotes=sample, limit_up=limit_up_list, broken=broken_list,
                breadth=None, prev=previous, source=sample[0].source if sample else "",
            )
            _save_emotion(payload["emotion"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("情绪计算失败")
            payload["degraded"].append({"block": "emotion", "reason": str(exc)})
            payload["emotion"] = None
    else:
        payload["emotion"] = None

    # 最近扫描结果
    from ..engines import STRATEGY_ORDER
    from ..engines.scanner import last_scan

    payload["latest_scans"] = {}
    for key in STRATEGY_ORDER:
        try:
            payload["latest_scans"][key] = last_scan(key)
        except Exception:  # noqa: BLE001
            payload["latest_scans"][key] = None

    if session.is_trading:
        payload["refresh_seconds"] = session.interval_seconds
    else:
        payload["refresh_seconds"] = 60 if session.should_poll else 0
    return payload


def _ensure_awaitable(coro: Any, name: str = "") -> Any:
    """把"可调用但还没调用"的协程函数就地求值。

    ⚠️ 这里防的是一个真实踩到过的坑: ``await bound_method`` 不会执行方法,
    而是**立刻返回方法对象本身**(不会报错!), 于是"可选数据块"永远拿到一个函数而不是数据,
    被 ``except Exception`` 静默吞掉 —— 表现为"某个面板长期为空但日志里什么都没有"。
    因此这里显式区分: 传进来的是协程 → 直接用; 是可调用对象 → 调用它。
    """
    if inspect.iscoroutine(coro):
        return coro
    if callable(coro):
        result = coro()
        if inspect.isawaitable(result):
            return result
        logger.warning("_safe 收到可调用对象 %s, 但其返回值不是可等待对象", name or coro)
        return _Immediate(result)
    if inspect.isawaitable(coro):
        return coro
    return _Immediate(coro)


class _Immediate:
    """把普通值包成可 await 的对象。"""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __await__(self):  # noqa: ANN201
        async def _inner() -> Any:
            return self.value

        return _inner().__await__()


async def _safe(coro: Any) -> Any:
    """执行一个"可选数据块", 失败时返回 None 并记录原因。

    传入协程或"返回协程的可调用对象"都支持(见 ``_ensure_awaitable``)。
    """
    try:
        return await _ensure_awaitable(coro, getattr(coro, "__name__", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("可选数据块失败(%s): %s", getattr(coro, "__name__", type(coro).__name__), exc)
        return None


async def _safe_one(coro: Any) -> Any:
    """取单个对象并转成 dict(用于行情/资金流等"单条"数据块)。"""
    result = await _safe(coro)
    if result is None:
        return None
    if isinstance(result, list):
        return result[0].as_dict() if result else None
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if hasattr(result, "value"):
        value = result.value
        return value.as_dict() if hasattr(value, "as_dict") else value
    return result


#: 情绪历史键
_EMOTION_KEY = "emotion_latest"


def _load_previous_emotion() -> dict[str, Any]:
    from ..store.db import db

    try:
        return db.kv_get(_EMOTION_KEY, {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_emotion(payload: dict[str, Any]) -> None:
    from ..store.db import db

    emotion = payload.get("emotion") or {}
    cycle = payload.get("cycle") or {}
    try:
        db.kv_set(_EMOTION_KEY, {
            "score": emotion.get("score"),
            "phase": cycle.get("phase"),
            "limit_up": (emotion.get("metrics") or {}).get("limit_up"),
            "max_consecutive": (emotion.get("metrics") or {}).get("max_consecutive"),
            "saved_at": time.time(),
            "date": today_str(),
        })
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 行情 / K线 / 分时
# --------------------------------------------------------------------------- #
async def quotes(codes: Sequence[str]) -> dict[str, Any]:
    if not codes:
        return {"items": [], "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        items = await registry.quotes([normalize_code(c) for c in codes])
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("quote", exc) from exc
    return {
        "items": [q.as_dict() for q in items],
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "source": items[0].source if items else "",
    }


async def search(keyword: str, limit: int = 20) -> dict[str, Any]:
    """按代码或名称检索。基于已缓存的全市场快照 —— 不再额外请求上游。"""
    text = (keyword or "").strip()
    if not text:
        return {"items": [], "keyword": text}
    try:
        snapshot = await registry.snapshot()
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("snapshot", exc) from exc
    upper = text.upper()
    hits: list[Quote] = []
    for quote in snapshot:
        if text in quote.code or upper in (quote.name or "").upper():
            hits.append(quote)
            if len(hits) >= limit * 3:
                break
    hits.sort(key=lambda q: (0 if q.code.startswith(text) else 1, -q.amount))
    return {
        "items": [q.as_dict() for q in hits[:limit]],
        "keyword": text,
        "total_scanned": len(snapshot),
    }


async def kline(code: str, days: int = 250, *, period: str = "day", with_indicators: bool = True) -> dict[str, Any]:
    """读取 K 线。

    磁盘里只缓存**日线**; 周线/月线由日线本地聚合(见 ``kline_store.aggregate_bars``),
    这样只维护一个数据口径, 不会出现"周线缓存与日线缓存不一致"导致的信号冲突。
    """
    code = normalize_code(code)
    kline_data: KLine | None = None
    origin = "network"
    if period == "day":
        cached = kline_store.get(code, days)
        if cached is not None and cached.bars:
            kline_data = cached
            origin = "disk"
    if kline_data is None:
        try:
            kline_data = await registry.kline(code, days, period=period)
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("kline", exc) from exc
        if kline_data is not None and period == "day":
            kline_store.put(kline_data, source=kline_data.source)
        elif kline_data is not None:
            origin = "aggregated" if getattr(kline_data, "source", "").endswith(period) else "network"
    if kline_data is None:
        raise _unavailable("kline", RuntimeError(f"{code} 无可用K线"))
    payload = kline_data.as_dict()
    payload["origin"] = origin
    if with_indicators and kline_data.bars:
        payload["indicators"] = tag_indicators(
            [b.high for b in kline_data.bars],
            [b.low for b in kline_data.bars],
            [b.close for b in kline_data.bars],
            [b.volume for b in kline_data.bars],
        )
    return payload


async def minute(code: str) -> dict[str, Any]:
    code = normalize_code(code)
    try:
        outcome = await registry.call("minute", code)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("minute", exc) from exc
    data = dict(outcome.value) if isinstance(outcome.value, dict) else {}
    data["requested_source"] = outcome.alias
    return data


async def stock_detail(code: str) -> dict[str, Any]:
    """个股详情: 行情 + K线 + 指标 + 资金流(可选)。"""
    code = normalize_code(code)
    payload: dict[str, Any] = {"code": code, "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S")}
    payload["quote"] = await _safe_one(registry.quotes([code]))
    payload["kline"] = await _safe(kline(code, 250))
    payload["money_flow"] = await _safe_one(registry.call("money_flow", code))
    return payload


# --------------------------------------------------------------------------- #
# 榜单 / 板块
# --------------------------------------------------------------------------- #
async def rank(kind: str = "gainers", limit: int = 50) -> dict[str, Any]:
    try:
        items = await registry.rank(kind, limit)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("rank", exc) from exc
    return {
        "kind": kind,
        "items": [q.as_dict() for q in items[:limit]],
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "source": items[0].source if items else "",
    }


async def sectors(kind: str = "industry") -> dict[str, Any]:
    try:
        items = await registry.sectors(kind)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("sector", exc) from exc
    return {
        "kind": kind,
        "items": [s.as_dict() for s in items],
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "source": items[0].source if items else "",
    }


async def sector_detail(sector_code: str, sector_name: str = "") -> dict[str, Any]:
    try:
        members = await registry.sector_members(sector_code, sector_name)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("sector_members", exc) from exc
    up = sum(1 for q in members if q.change_pct > 0)
    total = len(members)
    return {
        "code": sector_code,
        "name": sector_name,
        "member_count": total,
        "up_count": up,
        "up_ratio": round(up / total, 4) if total else 0.0,
        "avg_change_pct": round(float(np.mean([q.change_pct for q in members])), 3) if members else 0.0,
        "amount": sum(q.amount for q in members),
        "members": [q.as_dict() for q in sorted(members, key=lambda q: -q.change_pct)],
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def sector_flow(limit: int = 30) -> dict[str, Any]:
    try:
        outcome = await registry.call("sector_flow", limit)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("sector_flow", exc) from exc
    items = outcome.value if isinstance(outcome.value, list) else []
    return {
        "items": [s.as_dict() for s in items],
        "source": outcome.alias,
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def limit_up_pool() -> dict[str, Any]:
    try:
        pool = await registry.limit_up_pool()
    except Exception as exc:  # noqa: BLE001
        raise _unavailable("limit_up_pool", exc) from exc
    limit_up: list[LimitUpStock] = pool.get("limit_up") or []
    broken: list[LimitUpStock] = pool.get("broken") or []
    ladder: dict[int, int] = {}
    for stock in limit_up:
        level = max(1, stock.consecutive or 1)
        ladder[level] = ladder.get(level, 0) + 1
    return {
        "date": pool.get("date") or today_str().replace("-", ""),
        "limit_up_count": len(limit_up),
        "broken_count": len(broken),
        "broken_rate": round(len(broken) / (len(limit_up) + len(broken)) * 100, 2)
        if (limit_up or broken) else 0.0,
        "max_consecutive": max((s.consecutive or 1) for s in limit_up) if limit_up else 0,
        "ladder": {str(k): v for k, v in sorted(ladder.items())},
        "limit_up": [s.as_dict() for s in sorted(limit_up, key=lambda s: (-(s.consecutive or 1), -s.amount))],
        "broken": [s.as_dict() for s in broken],
        "source": pool.get("source", ""),
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------- #
# 资讯
# --------------------------------------------------------------------------- #
async def news(*, limit: int = 60, channel: str = "", force: bool = False) -> dict[str, Any]:
    """资讯聚合: 快讯 + 公告, 落库去重, 按保留期清理。"""
    from ..store.db import db

    fresh_items: list[Any] = []
    errors: list[str] = []
    if not channel or channel in ("news", "flash"):
        try:
            fresh_items.extend(await registry.news(limit))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"快讯: {exc}")
    if not channel or channel == "announcement":
        try:
            fresh_items.extend(await registry.announcements(min(40, limit)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"公告: {exc}")

    if fresh_items:
        _store_news(fresh_items)

    where = "WHERE channel=?" if channel else ""
    params: tuple[Any, ...] = (channel,) if channel else ()
    rows = db.query(
        f"SELECT * FROM news_item {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608 - where 由枚举控制
        (*params, max(1, min(300, limit))),
    )
    items = [
        {
            "id": row["id"], "title": row["title"], "summary": row["summary"],
            "url": row["url"], "source": row["source"], "channel": row["channel"],
            "published_at": row["published_at"],
            "related_codes": [c for c in str(row["related_codes"] or "").split(",") if c],
            "important": bool(row["important"]),
            "sentiment": row["sentiment"],
            "pushed": bool(row["pushed"]),
            "saved_at": row["created_at"],
        }
        for row in rows
    ]
    if not items:
        return {
            "items": [],
            "errors": errors,
            "hint": "尚未抓取到资讯; 请检查「数据源」页面的快讯/公告源是否可用",
            "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        }
    return {
        "items": items,
        "count": len(items),
        "errors": errors,
        "as_of": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _store_news(items: Iterable[Any]) -> int:
    """写入资讯表(按 id 幂等), 并标记重要消息。"""
    from ..store.db import db

    keywords = config().get("news.important_keywords", []) or []
    rows = []
    now = time.time()
    important_ids: list[str] = []
    for item in items:
        title = getattr(item, "title", "") or ""
        important = any(str(k) in title for k in keywords)
        if important:
            important_ids.append(item.id)
        rows.append((
            item.id, title, getattr(item, "summary", "") or "", getattr(item, "url", "") or "",
            getattr(item, "source", "") or "", getattr(item, "channel", "news"),
            str(getattr(item, "published_at", "") or ""),
            ",".join(getattr(item, "related_codes", []) or []),
            1 if important else 0,
            getattr(item, "sentiment", "neutral") or "neutral",
            1 if getattr(item, "pushed", False) else 0,
            now,
        ))
    if not rows:
        return 0
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO news_item(id, title, summary, url, source, channel, published_at, "
            "related_codes, important, sentiment, pushed, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "title=excluded.title, summary=excluded.summary, url=excluded.url, "
            "important=excluded.important",
            rows,
        )
    return len(rows)


def cleanup() -> dict[str, int]:
    """按保留期清理资讯等数据(定时任务调用, 也可在设置页手动触发)。"""
    from ..store.db import db

    retention = int(config().get("news.retention_days", 15) or 15)
    return db.cleanup(news_retention_days=retention)


# --------------------------------------------------------------------------- #
# 情绪
# --------------------------------------------------------------------------- #
async def emotion_panel() -> dict[str, Any]:
    """情绪面板 —— 复用仪表盘口径但只返回情绪部分。

    刻意**让并发请求共享同一份快照**：``asyncio.gather`` 同时拿快照与涨停池，
    注册中心的在途锁保证只拉一次全市场（否则定时任务与页面访问会各拉一遍，
    每次约 7 秒，还容易触发上游限流）。
    """
    quotes, pool = await asyncio.gather(
        registry.snapshot(),
        _safe(registry.limit_up_pool()),
        return_exceptions=True,
    )
    if isinstance(quotes, BaseException):
        raise _unavailable("snapshot", quotes)
    pool = pool if isinstance(pool, dict) else {}
    limit_n = config().universe_size
    sample = quotes if not limit_n else sorted(quotes, key=lambda q: -q.amount)[:limit_n]
    payload = emotion_engine.snapshot(
        quotes=sample, limit_up=pool.get("limit_up") or [],
        broken=pool.get("broken") or [], breadth=None,
        prev=_load_previous_emotion(), source=sample[0].source if sample else "",
    )
    payload["sample_limited"] = bool(limit_n)
    payload["universe_size"] = len(sample)
    _save_emotion(payload)
    return payload


def emotion_history(days: int = 30) -> dict[str, Any]:
    """情绪历史 —— 从行情/涨停记录里还原; 当前实现基于落库的扫描快照。"""
    from ..store.db import db

    rows = db.query(
        "SELECT trade_date, COUNT(*) AS n FROM scan_result "
        "GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
        (max(1, min(120, days)),),
    )
    return {
        "dates": [row["trade_date"] for row in rows],
        "latest": _load_previous_emotion(),
    }


__all__ = [
    "ServiceUnavailable",
    "clock",
    "indices",
    "breadth",
    "dashboard",
    "quotes",
    "search",
    "kline",
    "minute",
    "stock_detail",
    "rank",
    "sectors",
    "sector_detail",
    "sector_flow",
    "limit_up_pool",
    "news",
    "cleanup",
    "emotion_panel",
    "emotion_history",
]
