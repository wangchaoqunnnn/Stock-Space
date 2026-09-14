"""形态/因子策略库 —— 源自 stock-pattern-discovery 的策略库与因子库。

把 16 套经典入场形态统一成一个可扫描、可回测的引擎。每套形态:
  * ``entry`` 返回 ``(是否命中, 强度基础分, 依据列表, 关键指标)``;
  * 强度再经过 ``strength_of`` 的统一加成规则(量比 / 多头排列 / 是否已过度延伸);
  * 附带 ``exit`` 模板(ATR 止损倍数 / 止盈 / 时间止损 / 移动止盈触发 / 破线)。

统一加成规则(与原始实现一致, 便于口径对比):

    base(60/62/...) 
      + 量比 ≥2 → +18, ≥1.5 → +12, ≥1.2 → +6, <0.8 → -10
      + 多头排列(MA5>MA10>MA20 且 MA20 上行) → +10
      - 已过度延伸(偏离MA20 >15% 或 连涨 ≥4 日) → -15
      + 策略特定 extra
    最后夹到 [1, 100]
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .base import Condition, Series, Signal, Strategy, register
from .indicators import atr, boll, ema, macd, rsi, safe_float, sma


def _is_bull_aligned(series: Series) -> bool:
    ma5, ma10, ma20 = series.ma(5), series.ma(10), series.ma(20)
    if not (ma5 > ma10 > ma20 > 0):
        return False
    if series.length < 6:
        return False
    prev_ma20 = safe_float(series.ma20[-6])
    return ma20 > prev_ma20


def _is_extended(series: Series) -> bool:
    ma20 = series.ma(20)
    if ma20 > 0 and (series.close / ma20 - 1.0) > 0.15:
        return True
    # 连涨 ≥4 日
    if series.length < 5:
        return False
    closes = series.closes[-5:]
    return bool(np.all(np.diff(closes) > 0))


def _volume_ratio(series: Series, window: int = 5, offset: int = 1) -> float:
    """当日量 / 前 ``window`` 日均量(不含当日)。"""
    if series.length < window + offset:
        return 0.0
    segment = series.volumes[-(window + offset):-offset] if offset else series.volumes[-window:]
    base = float(np.mean(segment))
    return (series.volume / base) if base > 0 else 0.0


def strength_of(
    base: float, *, volume_ratio: float | None = None, aligned: bool = False,
    extended: bool = False, extra: float = 0.0,
) -> float:
    score = float(base)
    if volume_ratio is not None:
        if volume_ratio >= 2.0:
            score += 18
        elif volume_ratio >= 1.5:
            score += 12
        elif volume_ratio >= 1.2:
            score += 6
        elif volume_ratio < 0.8:
            score -= 10
    if aligned:
        score += 10
    if extended:
        score -= 15
    score += extra
    return max(1.0, min(100.0, round(score)))


# --------------------------------------------------------------------------- #
# 形态定义表
# --------------------------------------------------------------------------- #
PatternFn = Callable[[Series, Mapping[str, Any]], "tuple[bool, float, list[str], dict[str, Any]]"]


def _p_ma_volume_breakout(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    window = int(p.get("pivot_window", 60))
    if s.length < window + 2:
        return False, 0.0, [], {}
    prior_high = float(np.max(s.highs[-window - 1:-1]))
    vr = _volume_ratio(s)
    ok = (
        s.close >= prior_high * 0.995
        and s.close > s.opens[-1]
        and _is_bull_aligned(s)
        and vr >= 1.4
        and s.above_ma(20)
    )
    reasons = [f"收盘 {s.close:.2f} ≥ 近{window}日高点 {prior_high:.2f}×0.995",
               f"量比 {vr:.2f} ≥ 1.4", "多头排列且站上 MA20"]
    return ok, 68.0, reasons, {"volume_ratio": vr, "prior_high": prior_high}


def _p_ma_golden_cross(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    ma5 = sma(s.closes, 5)
    ma20 = sma(s.closes, 20)
    if s.length < 25 or np.isnan(ma5[-2]) or np.isnan(ma20[-2]):
        return False, 0.0, [], {}
    crossed = ma5[-2] <= ma20[-2] and ma5[-1] > ma20[-1]
    ma20_rising = ma20[-1] > ma20[-6] if not np.isnan(ma20[-6]) else False
    ok = bool(crossed and s.close > ma20[-1])
    base = 64.0 if ma20_rising else 50.0
    return ok, base, ["MA5 上穿 MA20", "MA20 走平转上" if ma20_rising else "MA20 尚未转上"], {}


def _p_macd_cross_zero(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 40:
        return False, 0.0, [], {}
    dif, dea, _ = macd(s.closes)
    crossed = dif[-2] <= dea[-2] and dif[-1] > dea[-1]
    above_zero = dif[-1] > 0
    ma60 = s.ma(60)
    base = 66.0 if (ma60 > 0 and s.close > ma60) else 52.0
    return bool(crossed and above_zero), base, [
        "DIF 上穿 DEA", "DIF > 0" if above_zero else "DIF 仍在 0 轴下方",
    ], {}


def _p_boll_breakout(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    upper, mid, lower = boll(s.closes, 20, 2.0)
    if s.length < 25 or np.isnan(upper[-2]):
        return False, 0.0, [], {}
    vr = _volume_ratio(s)
    bandwidth = (upper[-1] - lower[-1]) / mid[-1] if mid[-1] else 0.0
    historical = [
        (upper[i] - lower[i]) / mid[i]
        for i in range(max(0, s.length - 120), s.length)
        if mid[i] and not np.isnan(mid[i])
    ]
    narrow = bool(historical and bandwidth <= float(np.percentile(historical, 50)))
    ok = bool(s.close > upper[-1] and s.closes[-2] <= upper[-2] and vr >= 1.5)
    base = 70.0 if narrow else 60.0
    return ok, base, [
        f"收盘 {s.close:.2f} 突破布林上轨 {upper[-1]:.2f}",
        f"量比 {vr:.2f} ≥ 1.5",
        "带宽处于历史低位(收口后突破)" if narrow else "带宽未收口",
    ], {"bandwidth": bandwidth}


def _p_pivot_breakout(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    window = int(p.get("pivot_window", 20))
    if s.length < window + 2:
        return False, 0.0, [], {}
    prior_high = float(np.max(s.highs[-window - 1:-1]))
    atr_pct = (s.atr_value / s.close) if s.close > 0 else 1.0
    ok = bool(s.close > prior_high and atr_pct <= 0.06)
    return ok, 62.0, [
        f"收盘 {s.close:.2f} > 前{window}日最高 {prior_high:.2f}",
        f"ATR 占比 {atr_pct * 100:.2f}% ≤ 6%(低波动突破更可靠)",
    ], {"prior_high": prior_high, "atr_pct": atr_pct}


def _p_rsi_oversold_rebound(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 20:
        return False, 0.0, [], {}
    rsi6 = rsi(s.closes, 6)
    value = safe_float(rsi6[-1], 50.0)
    ma20 = s.ma(20)
    deviation = (s.close / ma20 - 1.0) if ma20 > 0 else 0.0
    low, high, close, open_price = s.lows[-1], s.highs[-1], s.close, s.opens[-1]
    span = max(high - low, 1e-9)
    lower_shadow = (min(close, open_price) - low) / span
    ok = bool(value < 22 and (close > open_price or lower_shadow > 0.5) and deviation <= -0.08)
    extra = 0.0
    if deviation < -0.15:
        extra += 8
    if value < 15:
        extra += 5
    return ok, 55.0 + extra, [
        f"RSI6 = {value:.1f} < 22(超卖)",
        f"偏离 MA20 {deviation * 100:.1f}% ≤ -8%",
        f"下影线占比 {lower_shadow:.2f}",
    ], {"rsi6": value, "deviation": deviation}


def _p_boll_lower_reversion(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    upper, mid, lower = boll(s.closes, 20, 2.0)
    if s.length < 25 or np.isnan(lower[-1]) or (upper[-1] - lower[-1]) == 0:
        return False, 0.0, [], {}
    percent_b = (s.close - lower[-1]) / (upper[-1] - lower[-1])
    ma60 = s.ma(60)
    above_ma60 = ma60 > 0 and s.close >= ma60 * 0.9
    vr = _volume_ratio(s)
    ok = bool(percent_b <= 0.05 and above_ma60)
    base = 62.0 if (ma60 > 0 and s.close > ma60) else 54.0
    if vr < 0.8:
        base += 6
    return ok, base, [
        f"%B = {percent_b:.2f} ≤ 0.05(触及下轨)",
        f"未跌破 MA60×0.9({above_ma60})",
        f"量比 {vr:.2f}(缩量更佳)",
    ], {"percent_b": percent_b}


def _p_limit_down_reversal(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 6:
        return False, 0.0, [], {}
    three_day = (s.closes[-1] / s.closes[-4] - 1.0) * 100.0 if s.closes[-4] else 0.0
    low, high, close, open_price = s.lows[-1], s.highs[-1], s.close, s.opens[-1]
    span = max(high - low, 1e-9)
    lower_shadow = (min(close, open_price) - low) / span
    vr = _volume_ratio(s)
    ok = bool(three_day <= -15 and close > open_price and lower_shadow > 0.25 and vr >= 1.2)
    extra = 6.0 if three_day < -22 else 0.0
    return ok, 58.0 + extra, [
        f"3日累计跌幅 {three_day:.1f}% ≤ -15%",
        f"下影线占比 {lower_shadow:.2f} > 0.25",
        f"量比 {vr:.2f} ≥ 1.2",
    ], {"three_day_pct": three_day}


def _p_volume_price_divergence(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 10:
        return False, 0.0, [], {}
    ma5v = sma(s.volumes, 5)
    base = safe_float(ma5v[-2])
    if base <= 0:
        return False, 0.0, [], {}
    shrink = float(np.mean(s.volumes[-5:-1])) / base
    ret5 = (s.closes[-1] / s.closes[-6] - 1.0) * 100.0 if s.closes[-6] else 0.0
    vr = _volume_ratio(s)
    ma20 = s.ma(20)
    ok = bool(
        shrink < 0.8 and ret5 <= -1.0 and s.close > s.opens[-1]
        and 1.05 <= vr <= 2.2 and ma20 > 0 and s.close >= ma20 * 0.97
    )
    return ok, 66.0, [
        f"前4日均量/前一日量能均线 = {shrink:.2f} < 0.8(缩量)",
        f"近5日回调 {ret5:.2f}%",
        f"今日量比 {vr:.2f} ∈ [1.05, 2.2]",
    ], {"shrink": shrink, "ret5": ret5}


def _p_volume_dry_up_breakout(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 65:
        return False, 0.0, [], {}
    ma5v = sma(s.volumes, 5)
    prev_ma5 = safe_float(ma5v[-2])
    lowest = float(np.min(s.volumes[-60:]))
    vr = _volume_ratio(s)
    ok = bool(
        prev_ma5 <= lowest * 1.35 and vr >= 1.8
        and s.close > s.opens[-1] and s.above_ma(20)
    )
    return ok, 68.0, [
        f"前一日量能均线 {prev_ma5:.0f} ≤ 近60日最低量 {lowest:.0f}×1.35(地量)",
        f"今日量比 {vr:.2f} ≥ 1.8(地量后放量)",
    ], {"prev_ma5_volume": prev_ma5, "lowest_volume": lowest}


def _p_relative_strength_leader(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 25:
        return False, 0.0, [], {}
    ret20 = s.ret(20)
    benchmark = p.get("_benchmark_ret20")
    if benchmark is None:
        return False, 0.0, ["缺少基准数据, 相对强度无法计算"], {}
    excess = ret20 - float(benchmark)
    ma10, ma20 = s.ma(10), s.ma(20)
    vr = _volume_ratio(s)
    ok = bool(
        ret20 >= 12 and excess >= 8 and ma10 > 0 and ma20 > 0
        and s.close <= ma10 * 1.02 and s.close >= ma20 * 0.97 and vr <= 1.6
    )
    base = 70.0 + min(10.0, excess * 0.5)
    return ok, base, [
        f"20日涨幅 {ret20:.1f}% ≥ 12%",
        f"超额收益 {excess:.1f}% ≥ 8%",
        f"回踩不破 MA20 且贴近 MA10(收盘 {s.close:.2f})",
    ], {"ret20": ret20, "excess": excess}


def _p_platform_breakout_retest(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 40:
        return False, 0.0, [], {}
    window = 15
    breakout_index: int | None = None
    for offset in range(1, window + 1):
        index = s.length - 1 - offset
        if index < 20:
            break
        prev_close = safe_float(s.closes[index - 1])
        if prev_close <= 0:
            continue
        pct = (s.closes[index] / prev_close - 1.0) * 100.0
        segment = s.volumes[max(0, index - 5):index]
        base = float(np.mean(segment)) if len(segment) else 0.0
        vr = (s.volumes[index] / base) if base > 0 else 0.0
        if pct > 5.0 and vr > 1.5:
            breakout_index = index
            break
    if breakout_index is None:
        return False, 0.0, ["近15日内无有效突破日"], {}
    pre = s.closes[max(0, breakout_index - 20):breakout_index]
    if len(pre) < 10:
        return False, 0.0, [], {}
    platform_high = float(np.max(pre))
    platform_low = float(np.min(pre))
    amplitude = (platform_high - platform_low) / platform_low if platform_low else 1.0
    if amplitude > 0.18:
        return False, 0.0, [f"突破前平台振幅 {amplitude * 100:.1f}% > 18%(不够收敛)"], {}
    ok = bool(
        platform_high * 0.97 <= s.close <= platform_high * 1.06
        and float(np.min(s.lows[breakout_index + 1:])) > platform_high * 0.95
    )
    return ok, 66.0, [
        f"平台高点 {platform_high:.2f}, 现价 {s.close:.2f} 在回踩区",
        f"平台振幅 {amplitude * 100:.1f}% ≤ 18%",
        "突破后低点未失守平台上沿×0.95",
    ], {"platform_high": platform_high}


def _p_main_wave_pullback(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 25:
        return False, 0.0, [], {}
    roc20 = s.ret(20) / 100.0
    ma5, ma10, ma20 = s.ma(5), s.ma(10), s.ma(20)
    vr = _volume_ratio(s)
    ok = bool(
        roc20 >= 0.20 and ma5 > ma10 > ma20 > 0
        and ma5 > 0 and abs(s.close / ma5 - 1.0) < 0.025
        and vr <= 1.3 and ma10 > 0 and s.lows[-1] > ma10 * 0.98
    )
    return ok, 72.0, [
        f"20日涨幅 {roc20 * 100:.1f}% ≥ 20%(主升已确认)",
        f"贴近 MA5({ma5:.2f})缩量回踩, 量比 {vr:.2f} ≤ 1.3",
        "未跌破 MA10×0.98",
    ], {"roc20": roc20, "volume_ratio": vr}


def _p_capitulation_volume_spike(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 5:
        return False, 0.0, [], {}
    prev_close = safe_float(s.closes[-2])
    pct = (s.close / prev_close - 1.0) * 100.0 if prev_close else 0.0
    vr = _volume_ratio(s)
    amount = safe_float(s.amounts[-1])
    ok = bool(pct <= -7.0 and vr >= 2.5 and amount >= 3e8)
    extra = 4.0 if pct < -9.0 else 0.0
    return ok, 56.0 + extra, [
        f"单日跌幅 {pct:.1f}% ≤ -7%(恐慌盘)",
        f"量比 {vr:.2f} ≥ 2.5, 成交额 {amount / 1e8:.2f} 亿 ≥ 3 亿",
    ], {"pct": pct, "volume_ratio": vr}


def _p_gap_up_hold(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 5:
        return False, 0.0, [], {}
    prev_high = safe_float(s.highs[-2])
    if prev_high <= 0:
        return False, 0.0, [], {}
    gap = (s.opens[-1] / prev_high - 1.0) * 100.0
    vr = _volume_ratio(s)
    ok = bool(
        1.0 <= gap <= 7.0 and s.lows[-1] > prev_high * 0.995
        and s.close >= s.opens[-1] * 0.99 and vr >= 1.3
    )
    return ok, 64.0, [
        f"跳空 {gap:.2f}% ∈ [1%, 7%]",
        f"回踩不破昨日高点 {prev_high:.2f}, 量比 {vr:.2f} ≥ 1.3",
    ], {"gap_pct": gap}


def _p_first_limit_up_confirm(s: Series, p: Mapping[str, Any]) -> tuple[bool, float, list[str], dict[str, Any]]:
    if s.length < 6:
        return False, 0.0, [], {}
    confirm_index: int | None = None
    for offset in range(1, 4):
        index = s.length - 1 - offset
        if index < 1:
            break
        prev_close = safe_float(s.closes[index - 1])
        if prev_close <= 0:
            continue
        pct = (s.closes[index] / prev_close - 1.0) * 100.0
        seal = s.closes[index] / s.highs[index] if s.highs[index] else 0.0
        if pct > 9.5 and seal > 0.995:
            confirm_index = index
            break
    if confirm_index is None:
        return False, 0.0, ["近3日无涨停确认日"], {}
    limit_close = safe_float(s.closes[confirm_index])
    vr = _volume_ratio(s)
    ok = bool(
        s.close >= limit_close * 0.97 and s.lows[-1] >= limit_close * 0.94 and vr <= 3.2
    )
    extra = 6.0 if s.volume < safe_float(s.volumes[confirm_index]) * 1.5 else 0.0
    return ok, 66.0 + extra, [
        f"{s.dates[confirm_index]} 涨停确认, 收盘价 {limit_close:.2f}",
        f"现价 {s.close:.2f} 未破涨停收盘×0.97",
        f"量比 {vr:.2f} ≤ 3.2",
    ], {"limit_close": limit_close}


#: 形态定义: key -> (中文名, 说明, 函数, exit 模板)
PATTERNS: dict[str, dict[str, Any]] = {
    "ma_volume_breakout": {
        "name": "均线放量突破", "fn": _p_ma_volume_breakout,
        "desc": "收盘创 60 日新高 + 收阳 + 多头排列 + 量比 ≥1.4",
        "exit": {"stop_atr": 2.0, "take_profit_pct": 18.0, "max_hold_days": 12,
                 "trail_after_pct": 8.0, "break_ma": 10},
    },
    "ma_golden_cross": {
        "name": "均线金叉", "fn": _p_ma_golden_cross,
        "desc": "MA5 上穿 MA20 且站上 MA20; MA20 走平转上时强度更高",
        "exit": {"stop_atr": 2.2, "take_profit_pct": 15.0, "max_hold_days": 15,
                 "trail_after_pct": 7.0, "break_ma": 20},
    },
    "macd_cross_zero": {
        "name": "MACD 零轴上穿", "fn": _p_macd_cross_zero,
        "desc": "DIF 上穿 DEA 且 DIF > 0",
        "exit": {"stop_atr": 2.4, "take_profit_pct": 20.0, "max_hold_days": 20,
                 "trail_after_pct": 9.0, "break_ma": 20},
    },
    "boll_breakout": {
        "name": "布林带突破", "fn": _p_boll_breakout,
        "desc": "收盘突破布林上轨 + 量比 ≥1.5; 带宽低位时强度更高",
        "exit": {"stop_atr": 1.8, "take_profit_pct": 14.0, "max_hold_days": 8,
                 "trail_after_pct": 6.0, "break_ma": 10},
    },
    "pivot_breakout": {
        "name": "枢轴突破", "fn": _p_pivot_breakout,
        "desc": "收盘突破前 20 日最高且 ATR 占比 ≤6%",
        "exit": {"stop_atr": 2.5, "take_profit_pct": 30.0, "max_hold_days": 30,
                 "trail_after_pct": 10.0, "break_ma": 20},
    },
    "rsi_oversold_rebound": {
        "name": "RSI 超卖反弹", "fn": _p_rsi_oversold_rebound,
        "desc": "RSI6 < 22 + 收阳或长下影 + 深度偏离 MA20",
        "exit": {"stop_atr": 1.5, "take_profit_pct": 7.0, "max_hold_days": 4,
                 "trail_after_pct": 4.0, "break_ma": 0},
    },
    "boll_lower_reversion": {
        "name": "布林下轨回归", "fn": _p_boll_lower_reversion,
        "desc": "%B ≤ 0.05 且未跌破 MA60×0.9, 缩量更佳",
        "exit": {"stop_atr": 1.8, "take_profit_pct": 8.0, "max_hold_days": 6,
                 "trail_after_pct": 5.0, "break_ma": 0},
    },
    "limit_down_reversal": {
        "name": "急跌反转", "fn": _p_limit_down_reversal,
        "desc": "3 日累计跌幅 ≤-15% + 收阳 + 长下影 + 放量",
        "exit": {"stop_atr": 1.6, "take_profit_pct": 9.0, "max_hold_days": 3,
                 "trail_after_pct": 5.0, "break_ma": 0},
    },
    "volume_price_divergence": {
        "name": "缩量企稳", "fn": _p_volume_price_divergence,
        "desc": "回调整理缩量 + 今日收阳温和放量",
        "exit": {"stop_atr": 2.0, "take_profit_pct": 16.0, "max_hold_days": 12,
                 "trail_after_pct": 8.0, "break_ma": 20},
    },
    "volume_dry_up_breakout": {
        "name": "地量后放量", "fn": _p_volume_dry_up_breakout,
        "desc": "前一日量能均线接近 60 日地量 + 今日量比 ≥1.8",
        "exit": {"stop_atr": 2.2, "take_profit_pct": 20.0, "max_hold_days": 20,
                 "trail_after_pct": 9.0, "break_ma": 20},
    },
    "relative_strength_leader": {
        "name": "强势股回踩", "fn": _p_relative_strength_leader,
        "desc": "20 日大幅跑赢 + 缩量回踩 MA10 不破 MA20",
        "exit": {"stop_atr": 2.2, "take_profit_pct": 22.0, "max_hold_days": 15,
                 "trail_after_pct": 9.0, "break_ma": 20},
    },
    "platform_breakout_retest": {
        "name": "平台突破回踩", "fn": _p_platform_breakout_retest,
        "desc": "收敛平台放量突破后回踩平台上沿不破",
        "exit": {"stop_atr": 2.0, "take_profit_pct": 20.0, "max_hold_days": 15,
                 "trail_after_pct": 8.0, "break_ma": 10},
    },
    "main_wave_pullback": {
        "name": "主升浪回踩", "fn": _p_main_wave_pullback,
        "desc": "20 日涨幅 ≥20% + 缩量回踩 MA5 不破 MA10",
        "exit": {"stop_atr": 2.0, "take_profit_pct": 18.0, "max_hold_days": 10,
                 "trail_after_pct": 8.0, "break_ma": 10},
    },
    "capitulation_volume_spike": {
        "name": "恐慌放量", "fn": _p_capitulation_volume_spike,
        "desc": "单日跌幅 ≤-7% + 量比 ≥2.5 + 成交额 ≥3 亿",
        "exit": {"stop_atr": 1.8, "take_profit_pct": 8.0, "max_hold_days": 3,
                 "trail_after_pct": 5.0, "break_ma": 0},
    },
    "gap_up_hold": {
        "name": "跳空不补", "fn": _p_gap_up_hold,
        "desc": "跳空 1%~7% 且回踩不破昨日高点",
        "exit": {"stop_atr": 1.8, "take_profit_pct": 10.0, "max_hold_days": 6,
                 "trail_after_pct": 6.0, "break_ma": 5},
    },
    "first_limit_up_confirm": {
        "name": "首板确认", "fn": _p_first_limit_up_confirm,
        "desc": "近 3 日涨停确认且现价未破涨停收盘×0.97",
        "exit": {"stop_atr": 2.5, "take_profit_pct": 12.0, "max_hold_days": 5,
                 "trail_after_pct": 7.0, "break_ma": 0},
    },
}


@register
class PatternStrategy(Strategy):
    """形态扫描引擎。``params['pattern']`` 决定使用哪一套形态。"""

    key = "pattern"
    name = "形态扫描（16 套经典形态）"
    category = "pattern"
    description = (
        "统一扫描 16 套经典入场形态(均线突破/金叉/MACD/布林/枢轴/超卖反弹/缩量企稳/"
        "地量放量/强势回踩/平台回踩/主升回踩/恐慌放量/跳空/首板确认), "
        "按统一规则加权重算强度, 并给出 ATR 止损/止盈/时间止损模板。"
    )
    source = "stock-pattern-discovery strategies/library.js"
    regime = "不同形态适配不同行情(见各形态说明与策略适配表)。"
    min_bars = 70

    PARAMS: dict[str, Any] = {
        "pattern": "ma_volume_breakout",
        "min_strength": 55.0,
        "pivot_window": 60,
        "exclude_limit": True,          # 剔除当日涨跌幅接近涨停/跌停的标的
        "min_amount": 50_000_000.0,     # 日均成交额下限(流动性)
    }
    PARAM_HINTS = {
        "pattern": ("使用形态", ""),
        "min_strength": ("入选强度门槛", "分"),
        "pivot_window": ("突破窗口", "日"),
        "min_amount": ("成交额下限", "元"),
    }

    # ------------------------------------------------------------------ #
    def evaluate(self, series: Series, params: Mapping[str, Any]) -> Signal:
        p = self.merged_params(params)
        pattern_key = str(p.get("pattern") or "ma_volume_breakout")
        spec = PATTERNS.get(pattern_key)
        if spec is None:
            return Signal(strategy=self.key, code=series.code, name=series.name,
                          score=0.0, passed=False,
                          reasons=[self.cond("形态存在", False, pattern_key, "已注册形态", 0.0)])
        if series.length < self.min_bars:
            return Signal(strategy=self.key, code=series.code, name=series.name,
                          score=0.0, passed=False, metrics={"bars": series.length},
                          reasons=[self.cond("K线充足", False, series.length, f"≥{self.min_bars}", 0.0)])

        # 流动性硬门槛
        amount_ma = float(np.mean(series.amounts[-20:])) if series.length >= 20 else 0.0
        if amount_ma and amount_ma < float(p["min_amount"]):
            return Signal(
                strategy=self.key, code=series.code, name=series.name, score=0.0, passed=False,
                metrics={"amount_ma20": amount_ma, "pattern": pattern_key},
                reasons=[self.cond("流动性充足", False, round(amount_ma, 0),
                                   f"≥ {p['min_amount']:.0f}", 0.0, "20日均成交额")],
            )

        # 剔除接近涨跌停的标的(次日开盘无法成交)
        if p["exclude_limit"]:
            threshold = series.limit_pct
            if abs(series.quote.change_pct) >= threshold - 0.2:
                return Signal(
                    strategy=self.key, code=series.code, name=series.name, score=0.0, passed=False,
                    metrics={"change_pct": series.quote.change_pct, "pattern": pattern_key},
                    reasons=[self.cond("非涨跌停", False, round(series.quote.change_pct, 2),
                                       f"|涨跌幅| < {threshold - 0.2:.1f}%", 0.0)],
                )

        hit, base, reasons_text, detail = spec["fn"](series, p)
        vr = _volume_ratio(series)
        aligned = _is_bull_aligned(series)
        extended = _is_extended(series)
        strength = strength_of(base, volume_ratio=vr, aligned=aligned, extended=extended)

        atr_value = series.atr_value or (series.close * 0.03)
        exit_spec = spec["exit"]
        stop = series.close - float(exit_spec["stop_atr"]) * atr_value
        target = series.close * (1 + float(exit_spec["take_profit_pct"]) / 100.0)

        conditions = [
            self.cond(f"形态·{spec['name']}", hit, spec["desc"], "命中入场条件", 0.60,
                      "；".join(reasons_text)[:200]),
            self.cond("量能配合", vr >= 1.2, round(vr, 2), "≥ 1.2", 0.15,
                      f"当日量/前5日均量 = {vr:.2f}"),
            self.cond("多头排列", aligned, None, "MA5>MA10>MA20 且 MA20 上行", 0.12),
            self.cond("未过度延伸", not extended, None, "偏离MA20 ≤15% 且 未连涨≥4日", 0.08),
            self.cond("流动性充足", amount_ma >= float(p["min_amount"]), round(amount_ma, 0),
                      f"≥ {p['min_amount']:.0f}", 0.05),
        ]
        score = self.score_from(conditions)
        # 强度反映形态质量, 条件分反映上下文, 各占一半
        blended = round(strength * 0.6 + score * 0.4, 2)
        # 入选判定只看「形态是否命中」与「强度门槛」——
        # 其余条件是逐条依据, 用于解释与排序, 不作为一票否决(否则用户会看到
        # 形态命中却因"量能未达 1.2 倍"整只被淘汰, 与"形态策略"的语义不符)。
        passed = bool(hit and strength >= float(p["min_strength"]))
        conditions.insert(0, self.cond(
            "是否入选", passed, round(strength, 1),
            f"形态命中 且 强度 ≥ {p['min_strength']}", 0.0,
            f"形态 {spec['name']} 命中={hit}；强度 {strength:.0f} / 门槛 {p['min_strength']}",
        ))

        return Signal(
            strategy=self.key, code=series.code, name=series.name,
            score=blended, passed=passed, reasons=conditions,
            metrics={
                "pattern": pattern_key, "pattern_name": spec["name"],
                "strength": round(strength, 1), "volume_ratio": round(vr, 3),
                "atr": round(atr_value, 3), "amount_ma20": round(amount_ma, 0),
                "detail": detail,
            },
            stop_loss=round(max(0.01, stop), 3),
            take_profit=round(target, 3),
            entry_low=round(series.close * 0.995, 3),
            entry_high=round(series.close * 1.005, 3),
            tags=[spec["name"]],
        )

    def rule_conditions(self, params: Mapping[str, Any]) -> list[Condition]:
        return [
            self.cond(spec["name"], True, spec["desc"], "命中入场条件", 1.0)
            for spec in PATTERNS.values()
        ]

    def doc(self) -> dict[str, Any]:
        """在基类文档基础上附加可选的形态清单。"""
        data = super().doc()
        data["patterns"] = self.pattern_catalog()
        return data

    def backtest_rules(self, params: Mapping[str, Any]) -> dict[str, Any]:
        p = self.merged_params(params)
        spec = PATTERNS.get(str(p.get("pattern")), PATTERNS["ma_volume_breakout"])
        exit_spec = spec["exit"]
        return {
            "stop_loss_pct": 8.0,
            "take_profit_pct": float(exit_spec["take_profit_pct"]),
            "max_hold_days": int(exit_spec["max_hold_days"]),
            "break_ma": int(exit_spec["break_ma"]),
            "use_atr_stop": True,
            "stop_atr": float(exit_spec["stop_atr"]),
            "trail_after_pct": float(exit_spec["trail_after_pct"]),
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def pattern_catalog() -> list[dict[str, Any]]:
        return [
            {"key": key, "name": spec["name"], "description": spec["desc"], "exit": spec["exit"]}
            for key, spec in PATTERNS.items()
        ]


__all__ = ["PatternStrategy", "PATTERNS", "strength_of"]
