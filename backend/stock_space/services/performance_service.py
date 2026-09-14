"""历史绩效复盘 —— 胜率 / 盈亏比 及其归因。

回答两类问题
============
1. **策略历史表现**（第6条）：过去某段时间里，某策略选出的票如果按它的风控模板
   执行，实际胜率与盈亏比是多少？差在哪？
2. **持仓历史表现**（第7、8条）：我的模拟持仓历史上胜率如何？平仓后立刻给结论。

为什么必须"按策略自己的出场规则"回放
====================================
用"持有 N 天"这种统一口径去评估所有策略是没有意义的 —— 策略的收益分布主要由
它的止损/止盈结构决定（2×ATR 止损与 8% 固定止损的胜率天生不同）。
因此这里直接复用 ``Backtester._check_exit``，即**回测引擎同一套离场逻辑**：
    止损(可 ATR) → 移动止盈 → 目标止盈 → 破线 → 时间止损
这样"历史胜率"才与"回测胜率"口径一致，两个页面的数字不会互相打架。

口径与限制（如实说明，不做美化）
================================
* 进场价取**扫描日之后第一个交易日的开盘价**（扫描在收盘后跑，当天无法买入）。
* 成本按 ``DEFAULT_COSTS``（佣金/印花税/滑点）计入，与回测页一致。
* 日线只有日频，因此同一根 bar 内止损与止盈同时触及时**一律按止损成交**
  （保守处理，避免结论虚高）。
* 未来数据严格截断：只用 ``entry_date`` 及之后的 bar。
* 若某只票在数据里找不到扫描日之后的行情，该笔**不计入**（而不是当作 0 收益）。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Sequence

from ..engines import get as get_strategy
from ..engines.backtest import DEFAULT_COSTS, Backtester, Trade, compute_metrics
from ..store.db import db
from ..store.kline_store import kline_store

logger = logging.getLogger(__name__)

#: 归因分桶
_HOLD_BUCKETS = ((0, 3, "≤3日"), (4, 7, "4~7日"), (8, 12, "8~12日"), (13, 999, ">12日"))
_SCORE_BUCKETS = ((0, 60, "<60分"), (60, 75, "60~75分"), (75, 85, "75~85分"), (85, 101, "≥85分"))


# --------------------------------------------------------------------------- #
# 单笔回放
# --------------------------------------------------------------------------- #
def _next_open(kline: Any, scan_date: str) -> tuple[int, float] | None:
    """扫描日之后**第一个交易日**的 (索引, 开盘价)。

    注意：这里要求**严格大于**扫描日 —— 扫描在收盘后执行，当天不可能买入。
    """
    for index, bar in enumerate(kline.bars):
        if str(bar.date) > str(scan_date):
            price = float(bar.open or 0.0)
            if price > 0:
                return index, price
    return None


def _has_future_bar(kline: Any, scan_date: str) -> bool:
    return any(str(bar.date) > str(scan_date) for bar in kline.bars)


async def _load_kline(code: str):
    """取日线：先磁盘，再网络。

    为什么需要网络兜底：扫描结果往往就落在**最新交易日**，而磁盘缓存的最新一根
    也正是那天 —— 严格按"入场后第 N 根"回放时会发现"后面没有数据"。
    若本地确实没有更新的数据，就该去上游补一次，否则整个历史复盘会得到 0 笔
    （实测踩过：200 笔全部被跳过）。
    """
    kline = kline_store.get(code, 400)
    if kline is not None and kline.bars:
        return kline
    try:
        from ..providers.registry import registry

        kline = await registry.kline(code, 400)
        if kline is not None and kline.bars:
            kline_store.put(kline, source=kline.source)
        return kline
    except Exception as exc:  # noqa: BLE001
        logger.debug("回放取日线失败 %s: %s", code, exc)
        return None


async def simulate_trade(
    *, code: str, name: str, strategy_key: str, scan_date: str, score: float = 0.0,
) -> Trade | None:
    """按策略的风控模板回放一笔（扫描日入场 → 触发出场）。"""
    strategy = get_strategy(strategy_key)
    if strategy is None:
        return None
    kline = await _load_kline(code)
    if kline is None or not kline.bars:
        return None

    entry = _next_open(kline, scan_date)
    if entry is None:
        return None
    entry_index, entry_price = entry
    if entry_index >= len(kline.bars):
        return None

    rules = strategy.backtest_rules({})
    backtester = Backtester(strategy=strategy)
    #: 建仓日 ATR（``_check_exit`` 的 ATR 止损读它）
    from ..engines.base import build_series
    from ..models import Quote

    bars_up_to = kline.bars[: entry_index + 1]
    bar = kline.bars[entry_index]
    prev = kline.bars[entry_index - 1] if entry_index >= 1 else bar
    live = Quote(
        code=code, name=name, price=bar.close, prev_close=prev.close,
        open=bar.open, high=bar.high, low=bar.low, change_pct=bar.change_pct,
        volume=bar.volume, amount=bar.amount,
    )
    from ..models import KLine

    sliced = KLine(code=code, name=name, period="day", bars=bars_up_to, source=kline.source)
    try:
        entry_atr = float(build_series(live, sliced).atr_value)
    except Exception:  # noqa: BLE001
        entry_atr = 0.0

    #: 含滑点的实际买入价（与回测引擎一致）
    buy_price = entry_price * (1 + float(DEFAULT_COSTS["slippage_rate"]))
    position: dict[str, Any] = {
        "entry_price": buy_price, "entry_atr": entry_atr,
        "peak_close": buy_price, "hold_days": 0,
    }
    max_gain = max_loss = 0.0
    exit_price = 0.0
    exit_reason = ""
    exit_index = entry_index

    for index in range(entry_index + 1, len(kline.bars)):
        bar = kline.bars[index]
        #: 逐根推进：hold_days / peak_close 必须先于离场判定更新
        position["hold_days"] = index - entry_index
        position["peak_close"] = max(float(position["peak_close"]), float(bar.close))
        gain = (float(bar.close) / buy_price - 1.0) * 100.0
        max_gain = max(max_gain, gain)
        max_loss = min(max_loss, gain)

        price, reason = backtester._check_exit(position, bar, kline, index, rules)
        if price is not None:
            exit_price, exit_reason, exit_index = float(price), reason, index
            break
    else:
        #: 未触发出场 → 用最后一根收盘价结清（与回测的"期末平仓"一致）
        last = kline.bars[-1]
        exit_price = float(last.close)
        exit_reason = "期末平仓"
        exit_index = len(kline.bars) - 1

    if exit_price <= 0:
        return None
    sell_price = exit_price * (1 - float(DEFAULT_COSTS["slippage_rate"]))
    #: 净收益：扣买卖两端的佣金，卖出再加印花税
    gross = sell_price / buy_price - 1.0
    cost = (float(DEFAULT_COSTS["commission_rate"]) * 2
            + float(DEFAULT_COSTS["stamp_duty_rate"]))
    pnl_pct = (gross - cost) * 100.0

    return Trade(
        code=code, name=name, strategy=strategy_key,
        entry_date=str(kline.bars[entry_index].date), entry_price=buy_price,
        shares=100.0, exit_date=str(kline.bars[exit_index].date), exit_price=exit_price,
        exit_reason=exit_reason, pnl_pct=round(pnl_pct, 3),
        pnl=round(pnl_pct, 3), hold_days=exit_index - entry_index,
        max_gain_pct=round(max_gain, 2), max_loss_pct=round(max_loss, 2), score=score,
    )


# --------------------------------------------------------------------------- #
# 归因
# --------------------------------------------------------------------------- #
def _bucket_of(value: float, buckets: Sequence[tuple[float, float, str]]) -> str:
    for low, high, label in buckets:
        if low <= value < high:
            return label
    return "其他"


def _equity_curve(trades: Sequence[Trade]) -> list[float]:
    """按平仓顺序累加收益率的**简化资金曲线**（初始 100）。

    为什么要有它：``compute_metrics`` 需要 equity 才能算回撤等指标。这里的持仓是
    一批独立信号而非一个有资金约束的组合，因此这条曲线只能反映"平均每笔收益的累积"，
    不是真实组合净值。使用它的指标（最大回撤/夏普/索提诺/卡玛）在本模块一律
    **不对外报告** —— 见 ``analyze()`` 里剔除的字段，避免给出误导性的风险指标。
    """
    equity = [100.0]
    for trade in sorted(trades, key=lambda t: (t.exit_date or "", t.code)):
        equity.append(equity[-1] * (1 + trade.pnl_pct / 100.0))
    return equity


def _metrics(trades: Sequence[Trade]) -> dict[str, Any]:
    """只保留对"信号质量"有意义的字段，剔除依赖组合资金曲线的风险指标。"""
    raw = compute_metrics(list(trades), _equity_curve(trades))
    return {
        k: raw.get(k) for k in (
            "trade_count", "closed_count", "win_count", "loss_count", "win_rate",
            "avg_win_pct", "avg_loss_pct", "payoff_ratio", "profit_factor",
            "expectancy_pct", "avg_hold_days", "gross_profit", "gross_loss",
        )
    }


def _group_stats(trades: Sequence[Trade], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[Trade]] = {}
    for trade in trades:
        groups.setdefault(str(key_fn(trade)), []).append(trade)
    out = []
    for label, items in groups.items():
        metrics = _metrics(items)
        out.append({
            "label": label,
            "count": len(items),
            "win_rate": metrics["win_rate"],
            "payoff_ratio": metrics["payoff_ratio"],
            "avg_pnl_pct": round(sum(t.pnl_pct for t in items) / len(items), 3),
            "avg_hold_days": metrics["avg_hold_days"],
            "pnl_sum": round(sum(t.pnl_pct for t in items), 2),
        })
    out.sort(key=lambda x: (-x["count"], x["label"]))
    return out


def analyze(trades: Sequence[Trade]) -> dict[str, Any]:
    """胜率/盈亏比 + 差异归因。"""
    if not trades:
        return {
            "metrics": _metrics([]),
            "attribution": {}, "suggestions": [],
            "best": None, "worst": None,
        }
    metrics = _metrics(trades)

    #: 行业：日线缓存里没有行业字段，用"代码段"代替会误导，
    #: 因此这里只用**代码前缀板块**做一个粗粒度对照，并明确标注是板块而非行业。
    def board_of(trade: Trade) -> str:
        code = trade.code
        if code.startswith(("600", "601", "603", "605")):
            return "沪主板"
        if code.startswith("688"):
            return "科创板"
        if code.startswith(("000", "001", "002", "003")):
            return "深主板"
        if code.startswith(("300", "301")):
            return "创业板"
        if code.startswith(("4", "8", "920")):
            return "北交所"
        return "其他"

    attribution = {
        "by_hold_days": _group_stats(trades, lambda t: _bucket_of(t.hold_days, _HOLD_BUCKETS)),
        "by_exit_reason": _group_stats(trades, lambda t: t.exit_reason or "未分类"),
        "by_score": _group_stats(trades, lambda t: _bucket_of(t.score, _SCORE_BUCKETS)),
        "by_board": _group_stats(trades, board_of),
        "by_entry_month": _group_stats(trades, lambda t: str(t.entry_date)[:7]),
    }

    ordered = sorted(trades, key=lambda t: t.pnl_pct)
    return {
        "metrics": metrics,
        "attribution": attribution,
        "suggestions": suggest(trades, metrics, attribution),
        "best": ordered[-1].as_dict() if ordered else None,
        "worst": ordered[0].as_dict() if ordered else None,
    }


def suggest(
    trades: Sequence[Trade], metrics: dict[str, Any], attribution: dict[str, Any],
) -> list[dict[str, str]]:
    """基于数据给出可执行的优化建议（只说实话，不硬凑条数）。"""
    out: list[dict[str, str]] = []
    total = len(trades)
    if not total:
        return out

    win_rate = float(metrics.get("win_rate") or 0.0)
    payoff = metrics.get("payoff_ratio")
    reasons = {row["label"]: row for row in attribution.get("by_exit_reason", [])}

    #: 1) 止损占比过高且亏损集中 → 止损可能过紧
    stop = reasons.get("止损")
    if stop and stop["count"] / total >= 0.4:
        total_loss = sum(t.pnl_pct for t in trades if t.pnl_pct <= 0)
        stop_loss = sum(t.pnl_pct for t in trades if t.exit_reason == "止损")
        share = abs(stop_loss / total_loss) if total_loss else 0.0
        out.append({
            "level": "high" if share >= 0.6 else "medium",
            "title": "止损过于频繁",
            "detail": f"止损占全部交易的 {stop['count'] / total:.0%}，"
                      f"贡献了 {share:.0%} 的亏损。多为止损后即反弹的洗盘，"
                      f"可考虑放宽 ATR 倍数，或改用『跌破前低』这类结构性止损。",
        })

    #: 2) 时间止损的平均收益为负 → 持有时长不合适
    tm = reasons.get("时间止损")
    if tm and tm["count"] >= 3 and tm["avg_pnl_pct"] < 0:
        out.append({
            "level": "medium",
            "title": "时间止损多为亏损离场",
            "detail": f"时间止损 {tm['count']} 笔，平均 {tm['avg_pnl_pct']:+.2f}%。"
                      f"说明持有到期限仍未走出来，可缩短最长持有天数，"
                      f"让资金更快转向更强的标的。",
        })

    #: 3) 盈亏比偏低 → 盈利单拿不住
    if payoff is not None and payoff < 1.5:
        out.append({
            "level": "high" if payoff < 1.0 else "medium",
            "title": "盈亏比偏低：赚小亏大",
            "detail": f"盈亏比 {payoff:.2f}（低于 1.5）。检查移动止盈的启动阈值："
                      f"过早启动回撤保护会把大波段截成小利。",
        })

    #: 4) 低分段拖累 → 提高门槛
    by_score = {row["label"]: row for row in attribution.get("by_score", [])}
    low = by_score.get("<60分")
    if low and low["count"] >= 3 and low["win_rate"] < win_rate:
        out.append({
            "level": "medium",
            "title": "低分标的拖累整体胜率",
            "detail": f"评分 <60 的 {low['count']} 笔胜率仅 {low['win_rate']:.0%}"
                      f"（整体 {win_rate:.0%}）。可上调买入评分门槛，"
                      f"或把低分单的仓位减半。",
        })

    #: 5) 整体表现
    if win_rate >= 0.5 and payoff is not None and payoff >= 1.5:
        out.append({
            "level": "info",
            "title": "整体表现健康",
            "detail": f"胜率 {win_rate:.0%}、盈亏比 {payoff:.2f}，"
                      f"期望收益 {metrics.get('expectancy_pct')}%。"
                      f"当前参数可继续使用，建议按月度复核。",
        })
    elif win_rate < 0.4:
        out.append({
            "level": "high",
            "title": "整体胜率偏低",
            "detail": f"胜率 {win_rate:.0%}。若盈亏比也不到 1.5，"
                      f"该参数组合不具备正期望，建议先收窄入场条件（提高评分门槛）"
                      f"而不是加大仓位。",
        })

    #: 6) 持有天数过短 → 可能被噪声打掉
    short = next((r for r in attribution.get("by_hold_days", []) if r["label"] == "≤3日"), None)
    if short and short["count"] / total >= 0.5 and short["win_rate"] < 0.4:
        out.append({
            "level": "medium",
            "title": "过半交易 3 日内即离场",
            "detail": f"≤3 日的 {short['count']} 笔胜率仅 {short['win_rate']:.0%}，"
                      f"多为日内噪声触发止损。可放宽初始止损或延后移动止盈的启动点。",
        })
    return out


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
async def review_scan_history(
    strategy_key: str, *, start: str = "", end: str = "", limit_days: int = 60,
) -> dict[str, Any]:
    """第6条：按策略回放历史扫描入选记录。"""
    if end:
        rows = db.query(
            "SELECT trade_date, code, name, score, payload FROM scan_result "
            "WHERE strategy=? AND trade_date<=? AND trade_date>=? "
            "ORDER BY trade_date DESC, rank",
            (strategy_key, end, start or "0000-00-00"),
        )[: max(1, limit_days) * 50]
    else:
        dates = db.query(
            "SELECT DISTINCT trade_date FROM scan_result WHERE strategy=? "
            "ORDER BY trade_date DESC LIMIT ?",
            (strategy_key, max(1, limit_days)),
        )
        day_list = [str(d["trade_date"]) for d in dates]
        if start:
            day_list = [d for d in day_list if d >= start]
        if not day_list:
            return {"strategy": strategy_key, "trades": [], "metrics": _metrics([]),
                    "attribution": {}, "suggestions": [], "days": []}
        placeholders = ",".join("?" for _ in day_list)
        rows = db.query(
            f"SELECT trade_date, code, name, score, payload FROM scan_result "
            f"WHERE strategy=? AND trade_date IN ({placeholders}) "
            f"ORDER BY trade_date DESC, rank",
            (strategy_key, *day_list),
        )

    #: 同一只票在同一天只算一笔；跨天重复入选各算一笔（真实建仓就是这样）
    trades: list[Trade] = []
    no_data = 0          #: 拿不到日线
    no_future = 0        #: 有日线但扫描日之后没有更新的 bar（数据太新）
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row["trade_date"]), str(row["code"]))
        if key in seen:
            continue
        seen.add(key)
        kline = await _load_kline(str(row["code"]))
        if kline is None or not kline.bars:
            no_data += 1
            continue
        if not _has_future_bar(kline, str(row["trade_date"])):
            #: 扫描日就是数据里最后一根 —— 没有"之后"的行情可用于评估，如实计入而非当作 0 收益
            no_future += 1
            continue
        trade = await simulate_trade(
            code=str(row["code"]), name=str(row["name"] or ""),
            strategy_key=strategy_key, scan_date=str(row["trade_date"]),
            score=float(row["score"] or 0.0),
        )
        if trade is None:
            no_data += 1
            continue
        trades.append(trade)

    result = analyze(trades)
    result.update({
        "strategy": strategy_key,
        "days": sorted({t.entry_date for t in trades}, reverse=True),
        "scanned": len(seen),
        "skipped": no_data + no_future,
        "skipped_no_data": no_data,
        "skipped_no_future_bars": no_future,
        "trades": [t.as_dict() for t in trades],
    })
    return result


def review_positions(*, strategy: str = "", limit: int = 500) -> dict[str, Any]:
    """第7条：模拟持仓历史回放（已平仓的直接用实际盈亏）。"""
    sql = "SELECT * FROM portfolio WHERE status='closed'"
    params: list[Any] = []
    if strategy:
        sql += " AND strategy=?"
        params.append(strategy)
    sql += " ORDER BY closed_at DESC LIMIT ?"
    params.append(int(limit))
    rows = db.query(sql, tuple(params))

    trades: list[Trade] = []
    for row in rows:
        entry = float(row["price"] or 0.0)
        close = float(row["close_price"] or 0.0)
        if entry <= 0 or close <= 0:
            continue
        opened = float(row["opened_at"] or 0.0)
        closed = float(row["closed_at"] or 0.0)
        hold = max(0, int((closed - opened) / 86400)) if closed > opened else 0
        trades.append(Trade(
            code=str(row["code"]), name=str(row["name"] or ""),
            strategy=str(row["strategy"] or "manual"),
            entry_date=_date_text(opened), entry_price=entry, shares=float(row["shares"] or 0),
            exit_date=_date_text(closed), exit_price=close,
            exit_reason="手动平仓",
            pnl_pct=float(row["pnl_pct"] or 0.0),
            pnl=float(row["pnl_pct"] or 0.0) * float(row["shares"] or 0),
            hold_days=hold, score=0.0,
        ))
    result = analyze(trades)
    result.update({"trades": [t.as_dict() for t in trades], "closed_count": len(trades)})
    return result


def review_single_position(position_id: int) -> dict[str, Any]:
    """第8条：平仓后立即结算这一笔，并给出针对性意见。"""
    row = db.query_one("SELECT * FROM portfolio WHERE id=?", (position_id,))
    if row is None:
        return {"found": False}
    entry = float(row["price"] or 0.0)
    close = float(row["close_price"] or 0.0) if row["status"] == "closed" else 0.0
    pnl_pct = float(row["pnl_pct"] or 0.0) if row["status"] == "closed" else 0.0
    hold = 0
    if row["closed_at"] and row["opened_at"]:
        hold = max(0, int((float(row["closed_at"]) - float(row["opened_at"])) / 86400))

    #: 同一策略的历史表现作为对照 —— 单笔无法谈"胜率"，必须放到样本里看
    context = review_positions(strategy=str(row["strategy"] or ""), limit=200)
    verdict = "盈" if pnl_pct > 0 else ("平" if abs(pnl_pct) < 0.01 else "亏")
    return {
        "found": True,
        "position": {
            "id": row["id"], "code": row["code"], "name": row["name"],
            "strategy": row["strategy"], "entry_price": entry, "close_price": close,
            "pnl_pct": round(pnl_pct, 3), "hold_days": hold, "verdict": verdict,
            "opened_at_text": _date_text(float(row["opened_at"] or 0)),
            "closed_at_text": _date_text(float(row["closed_at"] or 0)) if row["closed_at"] else "",
        },
        "context_metrics": context.get("metrics"),
        "suggestions": context.get("suggestions") or [],
        "strategy_sample": context.get("closed_count", 0),
    }


def _date_text(ts: float) -> str:
    import time

    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (ValueError, OSError):
        return ""


__all__ = [
    "simulate_trade", "analyze", "suggest",
    "review_scan_history", "review_positions", "review_single_position",
]
