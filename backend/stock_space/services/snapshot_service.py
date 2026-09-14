"""日终快照 —— 每个交易日收盘后写一次，记录自选池与模拟持仓的当日状态。

为什么需要它
============
原先库里只有"当前状态"与"开仓/平仓两个时点"：
  * 自选：只有 ``added_at``，移出即硬删除 → 历史完全丢失
  * 持仓：只有 ``opened_at`` / ``closed_at`` / ``pnl_pct``
    → 中间过程（"持有第 7 天涨了多少"）无从查起

于是"按日期查看历史"、"历史胜率/盈亏比回测"都缺少数据基础。
本模块把每天的状态**落成不可变的事实**，后续的日历、回测、归因都读它。

写入时机
========
由调度器在每个交易日收盘后执行一次（``scheduler.daily_job_hour/minute``，
默认 15:10）。**不做盘中每分钟落库** —— 按日一次即可满足回测需求，
且避免把数据库写成高频写入负载（约 5900 行/天的量级已经很可观）。

幂等性
======
两张快照表的主键都是 ``(trade_date, code/id)``，用 UPSERT 写入，
因此同一天重复执行只是覆盖，不会产生重复行（收盘后补跑也安全）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence

from ..core.util import now_cn, today_str
from ..models import Quote
from ..providers.registry import registry
from ..store.db import db
from ..store.kline_store import kline_store

logger = logging.getLogger(__name__)


async def _batch_quotes(codes: Sequence[str]) -> dict[str, Quote]:
    """批量取实时行情（失败不抛，返回已拿到的部分）。

    快照宁可"少几只"也不要整体失败 —— 失败的标的下一交易日会自然补上。
    """
    if not codes:
        return {}
    try:
        quotes = await registry.quotes(list(codes))
    except Exception as exc:  # noqa: BLE001
        logger.warning("快照取行情失败: %s", exc)
        return {}
    return {q.code: q for q in (quotes or [])}


def _close_on(code: str, trade_date: str) -> tuple[float, float, float, str]:
    """取某只股票**在指定交易日**的收盘价/前收/成交额，来源为本地日线缓存。

    为什么不能用实时行情补写历史：`registry.quotes()` 给的是**当前**价格。
    实测踩过——用今天的数据补写 2026-09-11 的快照，结果两天显示完全相同的
    价格与涨跌幅，日历看起来"能切日期"，其实数据是错的。
    历史日期必须用那一天的日线收盘价。

    返回 ``(close, prev_close, amount, source)``；取不到时返回全 0。
    """
    try:
        kline = kline_store.get(code, 20)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0, ""
    if kline is None or not kline.bars:
        return 0.0, 0.0, 0.0, ""
    for index, bar in enumerate(kline.bars):
        if str(bar.date) != str(trade_date):
            continue
        prev = kline.bars[index - 1].close if index >= 1 else bar.open
        change = ((bar.close / prev - 1.0) * 100.0) if prev else 0.0
        return float(bar.close), float(prev), float(bar.amount or 0.0), str(kline.source or "")
    return 0.0, 0.0, 0.0, ""


# --------------------------------------------------------------------------- #
# 自选池快照
# --------------------------------------------------------------------------- #
async def snapshot_watchlist(trade_date: str = "", *, fetch_quotes: bool = True,
                             historical: bool = False) -> dict[str, Any]:
    """把当前在池中的自选写成当日快照。

    ``historical=True`` 时用**当日日线收盘价**而非实时行情（见 ``_close_on``）。
    """
    day = trade_date or today_str()
    rows = db.query(
        "SELECT code, name FROM watchlist WHERE status<>'removed' ORDER BY code"
    )
    if not rows:
        return {"trade_date": day, "written": 0, "codes": 0}

    codes = [str(r["code"]) for r in rows]
    quotes = await _batch_quotes(codes) if (fetch_quotes and not historical) else {}
    now = time.time()
    payload = []
    for row in rows:
        code = str(row["code"])
        q = quotes.get(code)
        if historical:
            close, prev, amount, src = _close_on(code, day)
            if close <= 0:
                continue        #: 该日没有日线数据 → 不写，避免落一条全 0 的假快照
            change = ((close / prev - 1.0) * 100.0) if prev else 0.0
            #: 日线本身不带名称，优先用自选表里的名字；为空时留空而不是编造
            display_name = str(row["name"] or "").strip()
            payload.append((day, code, display_name, close, prev,
                            round(change, 4), amount, src, now))
            continue
        payload.append((
            day, code,
            (q.name if q else "") or str(row["name"] or ""),
            float(q.price) if q else 0.0,
            float(q.prev_close) if q else 0.0,
            float(q.change_pct) if q else 0.0,
            float(q.amount) if q else 0.0,
            (q.source if q else ""),
            now,
        ))
    if not payload:
        return {"trade_date": day, "written": 0, "codes": len(codes), "historical": historical}
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO watchlist_daily(trade_date, code, name, price, prev_close, "
            "change_pct, amount, source, captured_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date, code) DO UPDATE SET name=excluded.name, "
            "price=excluded.price, prev_close=excluded.prev_close, "
            "change_pct=excluded.change_pct, amount=excluded.amount, "
            "source=excluded.source, captured_at=excluded.captured_at",
            payload,
        )
    logger.info("自选池快照 %s: %d 只（取到行情 %d 只）", day, len(payload), len(quotes))
    return {"trade_date": day, "written": len(payload), "codes": len(codes),
            "with_quote": len(quotes)}


# --------------------------------------------------------------------------- #
# 模拟持仓快照
# --------------------------------------------------------------------------- #
async def snapshot_positions(trade_date: str = "", *, fetch_quotes: bool = True,
                            historical: bool = False) -> dict[str, Any]:
    """把**当前持有**的模拟持仓写成当日快照。

    关键字段 ``gain_pct`` = 自建仓价起的累计涨跌幅（用户明确要求的
    "每天实时统计自买入价位后的上涨/下跌幅度"）。

    ``historical=True`` 时用当日日线收盘价（补写历史）。
    """
    day = trade_date or today_str()
    rows = db.query("SELECT * FROM portfolio WHERE status='open' ORDER BY id")
    if not rows:
        return {"trade_date": day, "written": 0, "positions": 0}

    codes = [str(r["code"]) for r in rows]
    quotes = await _batch_quotes(codes) if (fetch_quotes and not historical) else {}
    now = time.time()
    payload = []
    for row in rows:
        code = str(row["code"])
        q = quotes.get(code)
        entry = float(row["price"] or 0.0)
        shares = float(row["shares"] or 0.0)
        if historical:
            close, _prev, _amt, src = _close_on(code, day)
            if close <= 0:
                continue        #: 该日无日线数据 → 跳过，避免假快照
        else:
            #: 取不到行情时退回建仓价（涨跌幅记 0），而不是写 0 造成"暴跌 100%"的假象
            close = float(q.price) if (q and q.price) else entry
            src = (q.source if q else "")
        gain_pct = ((close - entry) / entry * 100.0) if entry else 0.0
        market_value = close * shares
        payload.append((
            day, int(row["id"]), code, str(row["name"] or ""),
            entry, close, round(gain_pct, 4), round((close - entry) * shares, 2),
            shares, round(market_value, 2), _hold_days(row["opened_at"], day),
            "open", src, now,
        ))
    if not payload:
        return {"trade_date": day, "written": 0, "positions": len(rows),
                "historical": historical}
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO position_daily(trade_date, position_id, code, name, entry_price, "
            "close, gain_pct, gain_amount, shares, market_value, hold_days, status, "
            "source, captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date, position_id) DO UPDATE SET close=excluded.close, "
            "gain_pct=excluded.gain_pct, gain_amount=excluded.gain_amount, "
            "market_value=excluded.market_value, hold_days=excluded.hold_days, "
            "status=excluded.status, source=excluded.source, captured_at=excluded.captured_at",
            payload,
        )
    logger.info("模拟持仓快照 %s: %d 笔（取到行情 %d 只）", day, len(payload), len(quotes))
    return {"trade_date": day, "written": len(payload), "positions": len(rows),
            "with_quote": len(quotes)}


def _hold_days(opened_at: Any, trade_date: str) -> int:
    """持有交易日数（自然日近似：按日期差计）。"""
    try:
        opened = time.strftime("%Y-%m-%d", time.localtime(float(opened_at)))
    except (TypeError, ValueError, OSError):
        return 0
    try:
        import datetime as _dt

        a = _dt.date(*[int(x) for x in opened.split("-")])
        b = _dt.date(*[int(x) for x in trade_date.split("-")])
        return max(0, (b - a).days)
    except (ValueError, TypeError):
        return 0


# --------------------------------------------------------------------------- #
# 组合入口
# --------------------------------------------------------------------------- #
async def snapshot_all(trade_date: str = "", *, historical: bool | None = None) -> dict[str, Any]:
    """写当日全部快照（调度器与手工补跑都用它）。

    ``historical`` 决定用哪种价格：
      * None（默认）—— 自动判断：目标日期 == 今天就取实时行情，否则取当日日线收盘价。
      * True / False —— 强制指定（测试与脚本用）。

    自动判断这一点很关键：补写**历史日期**时若用了实时行情，会把今天的价格
    写进过去某一天，日历上看似正常、数据却是错的。
    """
    day = trade_date or today_str()
    if historical is None:
        historical = day != today_str()
    watch = await snapshot_watchlist(day, historical=historical)
    positions = await snapshot_positions(day, historical=historical)
    return {"trade_date": day, "historical": historical,
            "watchlist": watch, "positions": positions}


def available_dates(*, limit: int = 60) -> list[dict[str, Any]]:
    """哪些日期有快照 —— 日历只允许选这些日子（避免选到空数据）。"""
    rows = db.query(
        "SELECT trade_date, "
        "  (SELECT COUNT(*) FROM watchlist_daily w WHERE w.trade_date = d.trade_date) AS watch_codes, "
        "  (SELECT COUNT(*) FROM position_daily p WHERE p.trade_date = d.trade_date) AS positions "
        "FROM (SELECT DISTINCT trade_date FROM watchlist_daily "
        "      UNION SELECT DISTINCT trade_date FROM position_daily) d "
        "ORDER BY trade_date DESC LIMIT ?",
        (int(limit),),
    )
    return [
        {
            "trade_date": str(r["trade_date"]),
            "watch_codes": int(r["watch_codes"] or 0),
            "positions": int(r["positions"] or 0),
        }
        for r in rows
    ]


def day_snapshot(trade_date: str) -> dict[str, Any]:
    """某一天的自选池 + 持仓快照（日历按日查看用）。"""
    day = str(trade_date or "").strip()
    if not day:
        return {"trade_date": "", "watchlist": [], "positions": []}
    watch = db.query(
        "SELECT * FROM watchlist_daily WHERE trade_date=? ORDER BY change_pct DESC", (day,)
    )
    positions = db.query(
        "SELECT * FROM position_daily WHERE trade_date=? ORDER BY gain_pct DESC", (day,)
    )
    return {
        "trade_date": day,
        "watchlist": [dict(r) for r in watch],
        "positions": [dict(r) for r in positions],
    }


__all__ = [
    "snapshot_all", "snapshot_watchlist", "snapshot_positions",
    "available_dates", "day_snapshot",
]
