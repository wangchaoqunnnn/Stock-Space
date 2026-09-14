"""回测引擎。

设计目标: **口径保守、可解释、无未来函数**。

关键约定(逐条对应"回测最容易骗自己"的地方):

  1. **成交时点**: 默认 ``fill="next_open"`` —— 信号在 ``t`` 日收盘确认, ``t+1`` 日开盘成交。
     可切换为 ``fill="close"``(与部分原项目一致, 但同 bar 成交存在轻微前视), 界面会标注。
  2. **T+1**: 买入当日不可卖出, 由 ``entry_index < i`` 强制保证。
  3. **成本**: 佣金 0.023% + 印花税 0.05%(仅卖出) + 滑点 0.15%, 买入按 ``price×(1+滑点)``,
     卖出按 ``price×(1-滑点)`` 再扣佣金与印花税。
  4. **离场优先级**: 止损 → 移动止盈 → 止盈 → 破线 → 时间止损(与原始实现一致)。
     同一根 bar 内止损与止盈同时触发时 **一律按止损处理**(保守, 避免回测虚高)。
  5. **信号日与价格对齐**: 严格使用 ``bars[:i+1]`` 之前的数据计算指标, 绝不使用未来数据。
  6. **绩效年化按 244 个交易日**。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from ..core.util import limit_pct
from ..models import Bar, KLine, Quote
from .base import Series, Signal, Strategy, build_series
from .indicators import safe_float

logger = logging.getLogger(__name__)


def _rule(rules: Mapping[str, Any], key: str, default: Any) -> Any:
    """读取回测规则参数，**区分"没配置"与"显式配 0"**。

    直接用 `rules.get(key) or default` 是错的：Python 里 `0 or 8` 得到 8，
    于是"显式关闭该规则"(0) 会被当成"没配置"而套上默认值。
    止损/持有期/破线均线都以 0 表示"不启用"，必须原样尊重。
    """
    value = rules.get(key)
    return default if value is None else value

#: 默认成本(与 stock-pattern-discovery 一致, 该口径最完整)
DEFAULT_COSTS = {
    "commission_rate": 0.00023,
    "stamp_duty_rate": 0.0005,
    "slippage_rate": 0.0015,
}

#: 一年的交易日数(用于年化)
TRADING_DAYS = 244


@dataclass
class Trade:
    """一笔完整交易。"""

    code: str
    name: str = ""
    strategy: str = ""
    entry_date: str = ""
    entry_price: float = 0.0
    shares: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl: float = 0.0
    hold_days: int = 0
    max_gain_pct: float = 0.0
    max_loss_pct: float = 0.0
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "strategy": self.strategy,
            "entry_date": self.entry_date, "entry_price": round(self.entry_price, 3),
            "exit_date": self.exit_date, "exit_price": round(self.exit_price, 3),
            "exit_reason": self.exit_reason,
            "pnl_pct": round(self.pnl_pct, 3), "pnl": round(self.pnl, 2),
            "hold_days": self.hold_days,
            "max_gain_pct": round(self.max_gain_pct, 2),
            "max_loss_pct": round(self.max_loss_pct, 2),
            "score": round(self.score, 2),
        }


@dataclass
class BacktestResult:
    strategy: str
    start_date: str
    end_date: str
    universe_size: int = 0
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    cost_model: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_COSTS))
    fill_mode: str = "next_open"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self, *, trade_limit: int = 300) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "universe_size": self.universe_size,
            "fill_mode": self.fill_mode,
            "cost_model": self.cost_model,
            "metrics": self.metrics,
            "trades": [t.as_dict() for t in self.trades[-trade_limit:]],
            "trade_count": len(self.trades),
            "equity_curve": self.equity_curve,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# 指标计算
# --------------------------------------------------------------------------- #
def compute_metrics(trades: Sequence[Trade], equity: Sequence[float]) -> dict[str, Any]:
    """绩效指标。空样本时全部返回 0, 不返回 NaN。"""
    closed = [t for t in trades if t.exit_date]
    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]
    total = len(closed)
    win_rate = (len(wins) / total) if total else 0.0
    avg_win = float(np.mean([t.pnl_pct for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t.pnl_pct for t in losses])) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else (float("inf") if avg_win > 0 else 0.0)
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    equity_arr = np.asarray(list(equity), dtype=np.float64)
    total_return = (equity_arr[-1] / equity_arr[0] - 1.0) * 100.0 if len(equity_arr) > 1 and equity_arr[0] else 0.0
    peak = np.maximum.accumulate(equity_arr) if len(equity_arr) else np.array([1.0])
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.where(peak > 0, (peak - equity_arr) / peak, 0.0)
    max_dd = float(np.max(drawdown) * 100.0) if len(drawdown) else 0.0

    # 日收益序列(等权名义仓位)
    if len(equity_arr) > 1:
        with np.errstate(divide="ignore", invalid="ignore"):
            returns = np.diff(equity_arr) / equity_arr[:-1]
        returns = returns[np.isfinite(returns)]
    else:
        returns = np.array([])

    if len(returns) > 1:
        mean_r, std_r = float(np.mean(returns)), float(np.std(returns, ddof=1))
        sharpe = (mean_r / std_r * math.sqrt(TRADING_DAYS)) if std_r > 0 else 0.0
        downside = returns[returns < 0]
        downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
        sortino = (mean_r / downside_std * math.sqrt(TRADING_DAYS)) if downside_std > 0 else 0.0
        years = len(returns) / TRADING_DAYS
        annualized = ((equity_arr[-1] / equity_arr[0]) ** (1 / years) - 1) * 100.0 if years > 0 and equity_arr[0] > 0 else 0.0
    else:
        sharpe = sortino = annualized = 0.0

    calmar = (annualized / max_dd) if max_dd > 0 else 0.0
    avg_hold = float(np.mean([t.hold_days for t in closed])) if closed else 0.0

    # 按离场原因分组
    by_reason: dict[str, dict[str, Any]] = {}
    for trade in closed:
        bucket = by_reason.setdefault(trade.exit_reason or "未分类", {"count": 0, "win": 0, "pnl_sum": 0.0})
        bucket["count"] += 1
        bucket["win"] += 1 if trade.pnl_pct > 0 else 0
        bucket["pnl_sum"] += trade.pnl_pct
    for reason, bucket in by_reason.items():
        bucket["win_rate"] = round(bucket["win"] / bucket["count"], 4) if bucket["count"] else 0.0
        bucket["avg_pnl_pct"] = round(bucket["pnl_sum"] / bucket["count"], 3) if bucket["count"] else 0.0
        bucket["pnl_sum"] = round(bucket["pnl_sum"], 2)

    def _finite(value: float) -> float | None:
        return None if (math.isinf(value) or math.isnan(value)) else round(value, 4)

    return {
        "trade_count": len(trades),
        "closed_count": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "payoff_ratio": _finite(payoff),
        "profit_factor": _finite(profit_factor),
        "expectancy_pct": round((win_rate * avg_win + (1 - win_rate) * avg_loss), 3) if total else 0.0,
        "total_return_pct": round(total_return, 3),
        "annualized_return_pct": round(annualized, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "calmar": round(calmar, 3),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "avg_hold_days": round(avg_hold, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "by_exit_reason": by_reason,
    }


# --------------------------------------------------------------------------- #
# 回测执行
# --------------------------------------------------------------------------- #
class Backtester:
    """单策略回测器。

    用法::

        bt = Backtester(strategy, params, costs=DEFAULT_COSTS)
        result = bt.run(universe)          # universe: list[(Quote, KLine)]
    """

    def __init__(
        self,
        strategy: Strategy,
        params: Mapping[str, Any] | None = None,
        *,
        costs: Mapping[str, float] | None = None,
        fill: str = "next_open",
        initial_capital: float = 1_000_000.0,
        max_positions: int = 5,
        position_pct: float = 0.2,
        warmup_bars: int = 65,
        min_score: float | None = None,
        benchmark: str = "000300",
    ) -> None:
        self.strategy = strategy
        self.params = strategy.merged_params(params)
        self.costs = {**DEFAULT_COSTS, **(costs or {})}
        self.fill = fill if fill in ("next_open", "close") else "next_open"
        self.initial_capital = initial_capital
        self.max_positions = max(1, max_positions)
        self.position_pct = min(1.0, max(0.01, position_pct))
        self.warmup_bars = max(30, warmup_bars)
        self.min_score = min_score
        self.benchmark = benchmark

    # ------------------------------------------------------------------ #
    def run(self, universe: Sequence[tuple[Quote, KLine]]) -> BacktestResult:
        prepared = self._prepare(universe)
        if not prepared:
            return BacktestResult(
                strategy=self.strategy.key, start_date="", end_date="",
                metrics=compute_metrics([], [self.initial_capital]),
                fill_mode=self.fill,
                cost_model={k: float(v) for k, v in self.costs.items()},
                warnings=["没有足够的历史数据(需要至少 %d 根 K 线)" % self.warmup_bars],
            )

        dates = self._all_dates(prepared)
        if len(dates) <= self.warmup_bars:
            return BacktestResult(
                strategy=self.strategy.key,
                start_date=dates[0] if dates else "", end_date=dates[-1] if dates else "",
                metrics=compute_metrics([], [self.initial_capital]),
                fill_mode=self.fill,
                cost_model={k: float(v) for k, v in self.costs.items()},
                warnings=["交易日数量不足, 无法回测"],
            )

        index_by_code = {
            code: {bar.date: i for i, bar in enumerate(kline.bars)}
            for code, (_, kline) in prepared.items()
        }

        cash = self.initial_capital
        positions: dict[str, dict[str, Any]] = {}
        trades: list[Trade] = []
        pending: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        rules = self.strategy.backtest_rules(self.params)

        for step, date in enumerate(dates):
            if step < self.warmup_bars:
                continue

            # ---------- 1) 执行上一交易日产生的信号(默认次日开盘成交) ----------
            for order in pending:
                code = order["code"]
                price = self._fill_price(prepared[code][1], date, index_by_code[code])
                if price is None or code in positions:
                    continue
                if len(positions) >= self.max_positions:
                    break
                budget = min(cash, self.initial_capital * self.position_pct)
                if budget <= price * 100:
                    continue
                buy_price = price * (1 + float(self.costs["slippage_rate"]))
                shares = math.floor(budget / buy_price / 100) * 100
                if shares <= 0:
                    continue
                gross = shares * buy_price
                fee = gross * float(self.costs["commission_rate"])
                if gross + fee > cash:
                    continue
                cash -= gross + fee
                #: 记录建仓日的 ATR —— ``use_atr_stop`` 需要在建仓时就把止损距离
                #: 固定下来(2×ATR), 之后不再随波动率变化, 否则止损位会自己漂移。
                entry_atr = 0.0
                entry_series = self._series_until(kline, quote, date, index_by_code[code])
                if entry_series is not None:
                    entry_atr = safe_float(entry_series.atr_value)
                positions[code] = {
                    "code": code, "name": order["name"], "strategy": self.strategy.key,
                    "entry_date": date, "entry_price": buy_price, "shares": shares,
                    "entry_index": index_by_code[code].get(date, 0),
                    "peak_close": price, "entry_score": order["score"],
                    "entry_atr": entry_atr,
                    "hold_days": 0, "max_gain_pct": 0.0, "max_loss_pct": 0.0,
                }
            pending = []

            # ---------- 2) 离场检查(T+1: 当日买入不可卖) ----------
            for code in list(positions.keys()):
                position = positions[code]
                if position["entry_date"] >= date:
                    continue
                quote, kline = prepared[code]
                bar_index = index_by_code[code].get(date)
                if bar_index is None:
                    continue
                bar = kline.bars[bar_index]
                # 更新持仓统计(持有天数 / 峰值 / 期间最大浮盈浮亏)
                position["hold_days"] = bar_index - position["entry_index"]
                position["peak_close"] = max(position["peak_close"], bar.close)
                entry = position["entry_price"]
                if entry > 0:
                    position["max_gain_pct"] = max(
                        position.get("max_gain_pct", 0.0), (bar.high / entry - 1.0) * 100.0
                    )
                    position["max_loss_pct"] = min(
                        position.get("max_loss_pct", 0.0), (bar.low / entry - 1.0) * 100.0
                    )

                exit_price, reason = self._check_exit(position, bar, kline, bar_index, rules)
                if exit_price is None:
                    continue
                cash += self._close(position, exit_price, date, reason, trades)

            # ---------- 3) 生成今日信号 ----------
            if len(positions) + len(pending) < self.max_positions:
                for code, (quote, kline) in prepared.items():
                    if code in positions or any(o["code"] == code for o in pending):
                        continue
                    series = self._series_until(kline, quote, date, index_by_code[code])
                    if series is None:
                        continue
                    try:
                        signal = self.strategy.evaluate(series, self.params)
                    except Exception as exc:  # noqa: BLE001 - 单只异常不影响整体回测
                        logger.debug("回测评估 %s 失败: %s", code, exc)
                        continue
                    threshold = self.min_score if self.min_score is not None else 0.0
                    if signal.passed and signal.score >= threshold:
                        pending.append({
                            "code": code, "name": signal.name or quote.name,
                            "score": signal.score, "signal": signal,
                        })
                pending.sort(key=lambda o: -o["score"])
                pending = pending[: max(0, self.max_positions - len(positions))]

            # ---------- 4) 记录净值 ----------
            market_value = 0.0
            for code, position in positions.items():
                bar = self._bar_at(prepared[code][1], date, index_by_code[code])
                market_value += position["shares"] * (bar.close if bar else position["entry_price"])
            equity = cash + market_value
            equity_curve.append({"date": date, "equity": round(equity, 2),
                                 "positions": len(positions), "cash": round(cash, 2)})

        # ---------- 期末平仓 ----------
        last_date = dates[-1]
        for code in list(positions.keys()):
            position = positions[code]
            bar = self._bar_at(prepared[code][1], last_date, index_by_code[code])
            price = bar.close if bar else position["entry_price"]
            cash += self._close(position, price, last_date, "期末平仓", trades)

        equity_values = [e["equity"] for e in equity_curve] or [self.initial_capital]
        metrics = compute_metrics(trades, equity_values)
        warnings: list[str] = []
        if self.fill == "close":
            warnings.append("成交价采用信号日收盘价, 同 bar 成交存在轻微前视偏差; 建议使用次日开盘口径。")
        if metrics["closed_count"] < 20:
            warnings.append(f"样本仅 {metrics['closed_count']} 笔, 统计结论不稳健, 请扩大区间或放宽门槛。")
        return BacktestResult(
            strategy=self.strategy.key,
            start_date=dates[self.warmup_bars] if len(dates) > self.warmup_bars else dates[0],
            end_date=last_date,
            universe_size=len(prepared),
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            cost_model={k: float(v) for k, v in self.costs.items()},
            fill_mode=self.fill,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _prepare(self, universe: Sequence[tuple[Quote, KLine]]) -> dict[str, tuple[Quote, KLine]]:
        prepared: dict[str, tuple[Quote, KLine]] = {}
        for quote, kline in universe:
            if kline is None or len(kline.bars) < self.warmup_bars + 10:
                continue
            prepared[quote.code] = (quote, kline)
        return prepared

    @staticmethod
    def _all_dates(prepared: Mapping[str, tuple[Quote, KLine]]) -> list[str]:
        dates: set[str] = set()
        for _, kline in prepared.values():
            dates.update(bar.date for bar in kline.bars)
        return sorted(dates)

    @staticmethod
    def _bar_at(kline: KLine, date: str, index: Mapping[str, int]) -> Bar | None:
        position = index.get(date)
        if position is None:
            return None
        return kline.bars[position]

    def _fill_price(self, kline: KLine, date: str, index: Mapping[str, int]) -> float | None:
        position = index.get(date)
        if position is None:
            return None
        bar = kline.bars[position]
        # 一字涨停无法买入: 开盘即涨停且最低价等于最高价
        threshold = limit_pct(kline.code) / 100.0
        prev_close = kline.bars[position - 1].close if position > 0 else bar.open
        if prev_close > 0 and (bar.open / prev_close - 1.0) >= threshold - 0.005 and bar.high == bar.low:
            return None
        return bar.open if self.fill == "next_open" else bar.close

    def _series_until(
        self, kline: KLine, quote: Quote, date: str, index: Mapping[str, int]
    ) -> Series | None:
        """严格截断到 ``date``(含)的 K 线, 保证不使用未来数据。"""
        position = index.get(date)
        if position is None or position + 1 < self.warmup_bars:
            return None
        sliced = KLine(code=kline.code, name=kline.name, period=kline.period,
                       bars=kline.bars[: position + 1], source=kline.source)
        bar = kline.bars[position]
        live_quote = Quote(
            code=quote.code, name=quote.name, market=quote.market, board=quote.board,
            price=bar.close, prev_close=kline.bars[position - 1].close if position else bar.open,
            open=bar.open, high=bar.high, low=bar.low,
            change_pct=bar.change_pct, volume=bar.volume, amount=bar.amount,
            turnover_rate=bar.turnover_rate or quote.turnover_rate,
            total_mv=quote.total_mv, float_mv=quote.float_mv, is_st=quote.is_st,
            industry=quote.industry, source=kline.source,
        )
        return build_series(live_quote, sliced)

    def _check_exit(
        self, position: Mapping[str, Any], bar: Bar, kline: KLine, bar_index: int,
        rules: Mapping[str, Any],
    ) -> tuple[float | None, str]:
        """离场判定。优先级: 止损 → 移动止盈 → 目标止盈 → 破线 → 时间止损。

        同一根 bar 内止损与止盈同时可能触发时**一律按止损成交** —— 保守处理,
        避免回测因为"理想化成交"而虚高。
        """
        entry = float(position["entry_price"])
        #: ⚠️ 这里必须区分"没配置"与"显式配 0"。
        #: 原先写的是 `rules.get("stop_loss_pct") or 8.0` —— Python 里 `0.0 or 8.0`
        #: 求值为 **8.0**，于是显式声明 `stop_loss_pct: 0`（把止损完全交给 ATR）的
        #: 策略会**静默拿到 8% 固定止损**，2×ATR 那条路永远走不到。
        #: `max_hold_days` / `break_ma` 同理(0 表示不启用)。
        stop_pct = float(_rule(rules, "stop_loss_pct", 8.0)) / 100.0
        take_pct = float(_rule(rules, "take_profit_pct", 0.0)) / 100.0
        max_hold = int(_rule(rules, "max_hold_days", 20))
        break_ma = int(_rule(rules, "break_ma", 0))
        trail_after = float(_rule(rules, "trail_after_pct", 0.0)) / 100.0

        #: 初始止损优先用 ATR: 建仓日的 ATR 在买入时已固定, 这里只按倍数折算。
        #: 未声明 use_atr_stop、或建仓日 ATR 不可用时退回百分比止损 —— 两条路都要
        #: 保证 stop_price < entry, 否则会在第一根 bar 就被"止损"打掉。
        stop_price = entry * (1 - stop_pct)
        if bool(rules.get("use_atr_stop")):
            atr_value = float(position.get("entry_atr") or 0.0)
            multiple = float(rules.get("atr_multiple") or rules.get("stop_atr") or 2.0)
            if atr_value > 0 and multiple > 0:
                candidate = entry - multiple * atr_value
                #: 兜底: 2×ATR 过大时(如涨停次日)不允许止损超过 20%,
                #: 也不允许大于百分比止损位, 避免单笔风险失控
                floor = entry * (1 - max(stop_pct, 0.20))
                stop_price = max(candidate, floor)
            elif stop_pct <= 0:
                stop_price = 0.0   #: 既无 ATR 又没给百分比 → 视为不设止损

        # 1) 止损(跳空低开按开盘价成交)
        #: ⚠️ 必须先判断 stop_price > 0 再比较 —— 未配置止损时它是 0，
        #: 而 `bar.low <= 0` 恒为假本没问题，但若上游传来非正价格就会误触发。
        #: 显式判空更能表达"不设止损"这个意图。
        if stop_price > 0 and bar.low <= stop_price:
            return (bar.open if bar.open < stop_price else stop_price), "止损"
        # 2) 移动止盈: 浮盈先达到 trail_after 后, 从最高收盘回撤 6% 离场
        peak = float(position.get("peak_close") or entry)
        if trail_after > 0 and entry > 0 and (peak / entry - 1.0) >= trail_after:
            if bar.close <= peak * (1 - 0.06):
                return bar.close, "移动止盈"
        # 3) 目标止盈
        if take_pct > 0 and bar.high >= entry * (1 + take_pct):
            return entry * (1 + take_pct), "目标止盈"
        # 4) 破线离场(收盘跌破均线 × 0.985 缓冲)
        if break_ma > 0 and bar_index + 1 >= break_ma:
            segment = np.asarray(
                [b.close for b in kline.bars[bar_index + 1 - break_ma: bar_index + 1]],
                dtype=np.float64,
            )
            ma_value = float(np.mean(segment)) if len(segment) else 0.0
            if ma_value > 0 and bar.close < ma_value * 0.985:
                return bar.close, f"跌破MA{break_ma}"
        # 5) 时间止损
        if position.get("hold_days", 0) >= max_hold:
            return bar.close, "时间止损"
        return None, ""

    def _close(
        self, position: dict[str, Any], price: float, date: str, reason: str, trades: list[Trade]
    ) -> float:
        sell_price = price * (1 - float(self.costs["slippage_rate"]))
        gross = position["shares"] * sell_price
        fee = gross * (float(self.costs["commission_rate"]) + float(self.costs["stamp_duty_rate"]))
        cash = gross - fee
        entry_gross = position["shares"] * position["entry_price"]
        entry_fee = entry_gross * float(self.costs["commission_rate"])
        pnl = cash - (entry_gross + entry_fee)
        cost_basis = entry_gross + entry_fee
        trades.append(
            Trade(
                code=position["code"], name=position["name"], strategy=position["strategy"],
                entry_date=position["entry_date"], entry_price=position["entry_price"],
                shares=position["shares"], exit_date=date, exit_price=sell_price,
                exit_reason=reason,
                pnl_pct=(pnl / cost_basis * 100.0) if cost_basis else 0.0,
                pnl=pnl, hold_days=position.get("hold_days", 0),
                max_gain_pct=position.get("max_gain_pct", 0.0),
                max_loss_pct=position.get("max_loss_pct", 0.0),
                score=position.get("entry_score", 0.0),
            )
        )
        del position
        return cash


__all__ = ["Backtester", "BacktestResult", "Trade", "compute_metrics", "DEFAULT_COSTS"]
