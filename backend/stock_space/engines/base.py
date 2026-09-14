"""策略基类与登记表。

一个"策略"要实现四件事:
  1. ``meta()``       —— 名称、分类、说明、适用行情、出处;
  2. ``default_params()`` —— 全部阈值(必须可被用户在「设置」页覆盖);
  3. ``evaluate(ctx, code, params)`` —— 对单只股票打分, 返回 ``Signal``;
  4. ``backtest_rules()`` —— 给回测引擎的离场规则(止损/止盈/持有期)。

**评分一律归一化到 0~100**, 并附带逐条 ``reasons``(每条含名称/是否通过/实测值/阈值),
这样前端可以把"为什么入选"逐条展开 —— 这是多个原始项目的共同设计, 也是可解释性的底线。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from ..core.util import board_of, detect_market, limit_pct, normalize_code
from ..models import KLine, Quote
from .indicators import (
    atr,
    last_valid,
    ma_series,
    max_drawdown,
    pct_change,
    safe_float,
    slope,
    sma,
    to_array,
)


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #
@dataclass
class Condition:
    """单条判定条件 —— 前端逐条展开的依据。"""

    name: str
    passed: bool
    value: Any = None
    threshold: str = ""
    weight: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": bool(self.passed),
            "value": _jsonable(self.value),
            "threshold": self.threshold,
            "weight": round(self.weight, 4),
            "detail": self.detail,
        }


@dataclass
class Signal:
    """一只股票在某策略下的评估结果。"""

    strategy: str
    code: str
    name: str = ""
    score: float = 0.0
    passed: bool = False
    reasons: list[Condition] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    stop_loss: float = 0.0
    take_profit: float = 0.0
    entry_low: float = 0.0
    entry_high: float = 0.0
    tags: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.reasons if r.passed)

    @property
    def total_count(self) -> int:
        return len(self.reasons)

    @property
    def pass_ratio(self) -> float:
        return round(self.passed_count / self.total_count, 4) if self.reasons else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "code": self.code,
            "name": self.name,
            "score": round(self.score, 2),
            "passed": bool(self.passed),
            "passed_count": self.passed_count,
            "total_count": self.total_count,
            "pass_ratio": self.pass_ratio,
            "reasons": [r.as_dict() for r in self.reasons],
            "metrics": {k: _jsonable(v) for k, v in self.metrics.items()},
            "stop_loss": round(self.stop_loss, 3),
            "take_profit": round(self.take_profit, 3),
            "entry_low": round(self.entry_low, 3),
            "entry_high": round(self.entry_high, 3),
            "tags": list(self.tags),
            "note": self.note,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        return value.item()
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- #
# 单只标的的指标快照
# --------------------------------------------------------------------------- #
@dataclass
class Series:
    """一只股票的指标快照 —— 策略只读这里, 不再各自重复算指标。"""

    code: str
    name: str
    quote: Quote
    bars: list
    closes: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    volumes: np.ndarray
    amounts: np.ndarray
    dates: list[str]
    ma5: np.ndarray
    ma10: np.ndarray
    ma20: np.ndarray
    ma60: np.ndarray
    ma120: np.ndarray
    atr14: np.ndarray
    vol_ma5: np.ndarray
    vol_ma20: np.ndarray

    # ------------------------------ 便捷取值 ------------------------------
    @property
    def length(self) -> int:
        return len(self.closes)

    @property
    def close(self) -> float:
        return safe_float(self.closes[-1]) if self.length else 0.0

    @property
    def prev_close(self) -> float:
        return safe_float(self.closes[-2]) if self.length > 1 else self.close

    @property
    def volume(self) -> float:
        return safe_float(self.volumes[-1]) if self.length else 0.0

    @property
    def turnover_rate(self) -> float:
        return safe_float(self.quote.turnover_rate)

    def ma(self, window: int, offset: int = -1) -> float:
        series = {5: self.ma5, 10: self.ma10, 20: self.ma20, 60: self.ma60, 120: self.ma120}.get(window)
        if series is None or len(series) < abs(offset):
            return 0.0
        return safe_float(series[offset])

    def ret(self, periods: int) -> float:
        return pct_change(self.closes, periods)

    def max_drawdown(self, window: int = 20) -> float:
        return max_drawdown(self.closes[-window:])

    @property
    def atr_value(self) -> float:
        return last_valid(self.atr14)

    @property
    def board(self) -> str:
        return board_of(self.code)

    @property
    def limit_pct(self) -> float:
        return limit_pct(self.code, self.name)

    def max_single_day_gain(self, window: int = 10) -> float:
        """过去 ``window`` 日最大单日涨幅(%)。"""
        if self.length < 2:
            return 0.0
        segment = self.closes[-(window + 1):]
        with np.errstate(divide="ignore", invalid="ignore"):
            changes = np.diff(segment) / segment[:-1] * 100.0
        changes = changes[np.isfinite(changes)]
        return float(np.max(changes)) if len(changes) else 0.0

    def volume_ratio_5_20(self) -> float:
        v5 = safe_float(last_valid(self.vol_ma5))
        v20 = safe_float(last_valid(self.vol_ma20))
        return (v5 / v20) if v20 > 0 else 0.0

    def above_ma(self, window: int) -> bool:
        ma = self.ma(window)
        return bool(ma > 0 and self.close > ma)

    def position_in_range(self, window: int = 120) -> float:
        """当前价在近 ``window`` 日区间中的位置(0=最低, 1=最高)。"""
        if self.length < 2:
            return 0.5
        segment = self.closes[-window:]
        low, high = float(np.min(segment)), float(np.max(segment))
        if high <= low:
            return 0.5
        return float((self.close - low) / (high - low))


def build_series(quote: Quote, kline: KLine) -> Series:
    """由行情 + 日线构造指标快照。"""
    bars = kline.bars if kline else []
    closes = to_array([b.close for b in bars])
    opens = to_array([b.open for b in bars])
    highs = to_array([b.high for b in bars])
    lows = to_array([b.low for b in bars])
    volumes = to_array([b.volume for b in bars])
    amounts = to_array([b.amount for b in bars])
    mas = ma_series(closes, (5, 10, 20, 60, 120))
    return Series(
        code=quote.code,
        name=quote.name or (kline.name if kline else ""),
        quote=quote,
        bars=bars,
        closes=closes, opens=opens, highs=highs, lows=lows,
        volumes=volumes, amounts=amounts,
        dates=[b.date for b in bars],
        ma5=mas["ma5"], ma10=mas["ma10"], ma20=mas["ma20"],
        ma60=mas["ma60"], ma120=mas["ma120"],
        atr14=atr(highs, lows, closes, 14),
        vol_ma5=sma(volumes, 5),
        vol_ma20=sma(volumes, 20),
    )


# --------------------------------------------------------------------------- #
# 策略基类
# --------------------------------------------------------------------------- #
class Strategy:
    """策略基类。子类实现 ``evaluate`` 并在 ``PARAMS`` 声明可调参数。"""

    #: 唯一标识
    key: str = "base"
    #: 中文名
    name: str = "基础策略"
    #: 分类: trend / pattern / money / review
    category: str = "trend"
    #: 一句话说明
    description: str = ""
    #: 原始出处(项目名 + 文档), 便于溯源
    source: str = ""
    #: 适用行情
    regime: str = ""
    #: 最低K线根数要求
    min_bars: int = 70
    #: 参数默认值
    PARAMS: dict[str, Any] = {}
    #: 参数中文说明 {参数名: (说明, 单位)}
    PARAM_HINTS: dict[str, tuple[str, str]] = {}

    def meta(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "source": self.source,
            "regime": self.regime,
            "min_bars": self.min_bars,
            "params": self.merged_params({}),
            "param_defaults": dict(self.PARAMS),
            "param_hints": {k: {"label": v[0], "unit": v[1]} for k, v in self.PARAM_HINTS.items()},
        }

    def merged_params(self, overrides: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(self.PARAMS)
        for key, value in (overrides or {}).items():
            if key in params:
                try:
                    params[key] = type(params[key])(value)
                except (TypeError, ValueError):
                    params[key] = value
            else:
                params[key] = value
        return params

    # ------------------------------ 单只评估 ------------------------------
    def evaluate(self, series: Series, params: Mapping[str, Any]) -> Signal:  # pragma: no cover
        raise NotImplementedError

    def evaluate_quote(self, quote: Quote, kline: KLine, params: Mapping[str, Any]) -> Signal:
        return self.evaluate(build_series(quote, kline), params)

    # ------------------------------ 通用条件工具 ------------------------------
    @staticmethod
    def cond(name: str, passed: bool, value: Any = None, threshold: str = "",
             weight: float = 0.0, detail: str = "") -> Condition:
        return Condition(name=name, passed=bool(passed), value=value,
                         threshold=threshold, weight=weight, detail=detail)

    @staticmethod
    def score_from(conditions: Sequence[Condition], *, bonus: float = 0.0,
                   cap: float = 100.0) -> float:
        """按权重把条件通过情况折算成 0~100 分。

        权重为 0 的条件按等权处理 —— 这样"硬性一票否决"类条件也能参与打分,
        但不会因为某个权重大项不通过就直接 0 分(除非权重显式给满)。
        """
        weighted = [c for c in conditions if c.weight > 0]
        if not weighted:
            if not conditions:
                return 0.0
            ratio = sum(1 for c in conditions if c.passed) / len(conditions)
            return min(cap, max(0.0, ratio * 100.0 + bonus))
        total = sum(c.weight for c in weighted)
        earned = sum(c.weight for c in weighted if c.passed)
        return min(cap, max(0.0, earned / total * 100.0 + bonus))

    # ------------------------------ 回测规则 ------------------------------
    def backtest_rules(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """回测离场规则。默认: 止损 8%、无固定止盈、最长持有 20 个交易日。"""
        return {
            "stop_loss_pct": 8.0,
            "take_profit_pct": 0.0,
            "max_hold_days": 20,
            "break_ma": 20,
            "use_atr_stop": False,
        }

    # ------------------------------ 说明文档 ------------------------------
    def doc(self) -> dict[str, Any]:
        return {
            **self.meta(),
            "logic": self.description,
            "rules": [c.as_dict() for c in self.rule_conditions({})],
            "backtest": self.backtest_rules({}),
        }

    def rule_conditions(self, params: Mapping[str, Any]) -> list[Condition]:  # pragma: no cover
        return []


# --------------------------------------------------------------------------- #
# 登记表
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, Strategy] = {}


def register(strategy: "Strategy | type[Strategy]") -> Any:
    """登记一个策略。

    既支持 ``@register`` 直接装饰**实例**(``@register`` + ``obj = XxxStrategy()`` 不可行,
    因为装饰发生在类定义时), 也支持装饰类 —— 后者会自动实例化, 这是各策略模块采用的写法:

        @register
        class TrendStrategy(Strategy):
            ...
    """
    if isinstance(strategy, type):
        instance = strategy()
        _REGISTRY[instance.key] = instance
        return strategy
    _REGISTRY[strategy.key] = strategy
    return strategy


def get(key: str) -> Strategy | None:
    return _REGISTRY.get(key)


def all_strategies() -> list[Strategy]:
    return list(_REGISTRY.values())


def keys() -> list[str]:
    return list(_REGISTRY.keys())


__all__ = [
    "Condition",
    "Signal",
    "Series",
    "Strategy",
    "build_series",
    "register",
    "get",
    "all_strategies",
    "keys",
]
