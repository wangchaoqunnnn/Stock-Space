"""策略引擎包: 登记所有策略并编排扫描。

策略清单(11 个原始项目的能力归一化后):

    trend                   趋势狙击            TrendSniper
    quiet_rise              悄悄上涨(潜涨雷达)  QuietRiseScanner
    limit_up_pullback       涨停回调低吸        Limit-Up-Pullback-Buy-Setup
    n_pattern               N 字战法            NPatternStrategy
    pattern                 形态扫描(16 套)     stock-pattern-discovery
    volume_shrink_rebound   缩量回调后温和放量  用户自定义(交易心法)
    emotion                 市场情绪周期        92KeBi + StockTradingReviewTool

导入本包即完成策略注册(``register`` 装饰器在模块导入时执行)。
"""

from __future__ import annotations

from .base import (
    Condition,
    Series,
    Signal,
    Strategy,
    all_strategies,
    build_series,
    get,
    keys,
    register,
)
from .indicators import tag_indicators
from .pattern import PATTERNS, PatternStrategy
from .backtest import Backtester, BacktestResult, Trade, compute_metrics
from .emotion import EmotionContext, snapshot as emotion_snapshot
from .limit_up_pullback import LimitUpPullbackStrategy
from .n_pattern import NPatternStrategy
from .quiet_rise import QuietRiseStrategy
from .trend import TrendStrategy
from .volume_shrink_rebound import VolumeShrinkReboundStrategy

#: 策略展示顺序(与需求文档的功能清单一致)
STRATEGY_ORDER = (
    "trend", "quiet_rise", "limit_up_pullback", "n_pattern", "pattern",
    "volume_shrink_rebound",
)


def catalog() -> list[dict]:
    """按展示顺序返回策略元信息, 并附带每个策略的默认参数与说明。"""
    strategies = {s.key: s for s in all_strategies()}
    out: list[dict] = []
    for key in STRATEGY_ORDER:
        strategy = strategies.get(key)
        if strategy is None:
            continue
        meta = strategy.meta()
        meta["rules"] = [c.as_dict() for c in strategy.rule_conditions({})]
        meta["backtest"] = strategy.backtest_rules({})
        if key == "pattern":
            meta["patterns"] = PatternStrategy.pattern_catalog()
        out.append(meta)
    for key, strategy in strategies.items():
        if key not in STRATEGY_ORDER:
            out.append(strategy.meta())
    return out


__all__ = [
    "Strategy", "Signal", "Condition", "Series", "build_series",
    "register", "get", "all_strategies", "keys", "catalog",
    "TrendStrategy", "QuietRiseStrategy", "LimitUpPullbackStrategy",
    "NPatternStrategy", "PatternStrategy", "PATTERNS",
    "VolumeShrinkReboundStrategy",
    "Backtester", "BacktestResult", "Trade", "compute_metrics",
    "EmotionContext", "emotion_snapshot", "tag_indicators",
    "STRATEGY_ORDER",
]
