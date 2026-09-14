"""悄悄上涨(潜涨)评分 —— 源自 QuietRiseScanner。

核心思想: 一只股票在启动前往往不是以涨停/放巨量/上热搜的方式出现, 而是
「价格沿均线缓慢抬升 + 成交量温和放大 + 波动率低 + 关注度不高 + 板块内多股同步走强」。

评分模型(与需求文档一致):

    QuietRiseScore = 0.30×Trend + 0.20×Volume + 0.20×LowVol
                   + 0.15×RelativeStrength + 0.15×Attention (+ 板块共振加分 5~10)

**子分全部使用分段线性/带宽连续函数, 不是 0/1 判定** —— 这一点很关键:
它让"接近达标"的股票也能得到中间分, 从而在排序上有意义, 也便于调参。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from .base import Condition, Series, Signal, Strategy, register
from .indicators import safe_float, slope


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _band(value: float, low: float, high: float, *, soft: float = 1.0) -> float:
    """带宽打分: 落在 ``[low, high]`` 得 100, 越界线性衰减到 0。"""
    if low <= value <= high:
        return 100.0
    span = max(abs(high - low), 1e-9) * soft
    distance = (low - value) if value < low else (value - high)
    return _clamp(100.0 * (1.0 - distance / span))


def _linear(value: float, best: float, worst: float) -> float:
    """``value`` 从 ``best``(100 分) 线性衰减到 ``worst``(0 分)。"""
    if abs(worst - best) < 1e-9:
        return 100.0 if value >= best else 0.0
    ratio = (value - best) / (worst - best)
    return _clamp(100.0 * (1.0 - ratio))


@register
class QuietRiseStrategy(Strategy):
    key = "quiet_rise"
    name = "悄悄上涨（潜涨雷达）"
    category = "trend"
    description = (
        "把「沿均线缓慢抬升、温和放量、低波动、跑赢大盘但不过热、市场关注度不高」"
        "翻译成五维连续评分, 总分 >70 进入潜涨观察池; 板块共振额外加 5~10 分。"
    )
    source = "QuietRiseScanner《requirements.md》+ screening-rules"
    regime = "适用于震荡市与慢牛初期; 主跌段会出现大量假信号, 建议只做观察不做逻辑定价。"
    min_bars = 130

    PARAMS: dict[str, Any] = {
        # 权重
        "w_trend": 0.30,
        "w_volume": 0.20,
        "w_low_vol": 0.20,
        "w_relative_strength": 0.15,
        "w_attention": 0.15,
        # 趋势
        "change_20d_min": 3.0,
        "change_20d_max": 30.0,
        "ma20_slope_min_pct": 0.15,
        "ma_slope_lookback": 5,
        # 量能
        "vol_ratio_min": 1.0,
        "vol_ratio_max": 2.5,
        "turnover_min": 1.0,
        "turnover_max": 8.0,
        # 波动
        "atr_pct_max": 4.0,
        "max_single_day_gain": 7.0,
        "max_drawdown_20d": 10.0,
        "single_day_window": 10,
        # 相对强度
        "excess_min": 0.0,
        "excess_max": 20.0,
        # 关注度
        "attention_default": 60.0,
        "popularity_top_n": 100,
        # 基础预筛
        "exclude_st": True,
        "min_market_cap": 5_000_000_000.0,   # 50 亿
        "min_amount": 100_000_000.0,          # 1 亿
        # 板块共振
        "resonance_bonus_min": 5.0,
        "resonance_bonus_max": 10.0,
        "resonance_member_ratio": 0.10,
        # 入选门槛
        "watchlist_threshold": 70.0,
    }
    PARAM_HINTS = {
        "change_20d_min": ("20日涨幅下界", "%"),
        "change_20d_max": ("20日涨幅上界", "%"),
        "vol_ratio_min": ("量比下界", ""),
        "vol_ratio_max": ("量比上界", ""),
        "turnover_min": ("换手下界", "%"),
        "turnover_max": ("换手上界", "%"),
        "atr_pct_max": ("ATR/价格上限", "%"),
        "max_drawdown_20d": ("20日最大回撤上限", "%"),
        "excess_max": ("超额收益上界", "%"),
        "watchlist_threshold": ("观察池门槛", "分"),
    }

    # ------------------------------------------------------------------ #
    def evaluate(self, series: Series, params: Mapping[str, Any]) -> Signal:
        p = self.merged_params(params)
        reasons: list[Condition] = []
        metrics: dict[str, Any] = {}
        close = series.close

        if series.length < self.min_bars:
            return Signal(
                strategy=self.key, code=series.code, name=series.name, score=0.0,
                passed=False, metrics={"bars": series.length},
                reasons=[self.cond("K线充足", False, series.length, f"≥{self.min_bars}", 0.0)],
            )

        # ---------------- 基础预筛 ----------------
        prescreen_issues: list[str] = []
        if p["exclude_st"] and series.quote.is_st:
            prescreen_issues.append("ST 标的")
        market_cap = safe_float(series.quote.total_mv)
        if 0 < market_cap < p["min_market_cap"]:
            prescreen_issues.append(f"总市值 {market_cap / 1e8:.2f}亿 < {p['min_market_cap'] / 1e8:.0f}亿")
        amount = safe_float(series.quote.amount)
        if 0 < amount < p["min_amount"]:
            prescreen_issues.append(f"成交额 {amount / 1e8:.2f}亿 < {p['min_amount'] / 1e8:.0f}亿")
        if close <= 0:
            prescreen_issues.append("无有效报价")

        # ---------------- 1) 趋势分 ----------------
        ma20, ma60 = series.ma(20), series.ma(60)
        change_20d = series.ret(20)
        slope_lookback = int(p["ma_slope_lookback"])
        if series.length > slope_lookback:
            valid_ma20 = series.ma20[~np.isnan(series.ma20)]
            ma20_slope = slope(valid_ma20[-slope_lookback:], slope_lookback)
        else:
            ma20_slope = 0.0

        trend_parts: list[tuple[float, float]] = []   # (满分, 实得)
        # 价 > MA20 且 MA20 > MA60 (30 分)
        above = bool(ma20 > 0 and close > ma20)
        above_long = bool(ma60 > 0 and ma20 > ma60)
        if above and above_long:
            trend_parts.append((30.0, 30.0))
        elif above:
            trend_parts.append((30.0, 15.0))
        else:
            deviation = (close / ma20 - 1.0) * 100.0 if ma20 > 0 else -5.0
            trend_parts.append((30.0, _clamp(30.0 * (1 + deviation / 5.0), 0.0, 30.0)))
        # MA20 斜率 (25 分)
        min_slope = float(p["ma20_slope_min_pct"])
        if ma20_slope >= min_slope:
            trend_parts.append((25.0, 25.0))
        elif ma20_slope > 0:
            trend_parts.append((25.0, 25.0 * (0.5 + 0.5 * ma20_slope / max(min_slope, 1e-6))))
        else:
            trend_parts.append((25.0, _clamp(12.5 * (1 + ma20_slope / 3.0), 0.0, 12.5)))
        # 20日涨幅区间 (20 分)
        low_r, high_r = float(p["change_20d_min"]), float(p["change_20d_max"])
        if low_r <= change_20d <= high_r:
            trend_parts.append((20.0, 20.0))
        elif change_20d < low_r:
            trend_parts.append((20.0, _clamp(20.0 * (1 - (low_r - change_20d) / max(abs(low_r), 1e-6)))))
        else:
            trend_parts.append((20.0, _clamp(20.0 * (1 - (change_20d - high_r) / 20.0))))
        # 偏离 MA20 惩罚 (25 分中扣)
        deviation_pct = (close / ma20 - 1.0) * 100.0 if ma20 > 0 else 0.0
        extension_penalty = min(10.0, max(0.0, (deviation_pct - 15.0) * 0.5))
        trend_score = max(0.0, sum(e for _, e in trend_parts) - extension_penalty)

        metrics.update({
            "change_20d": round(change_20d, 3),
            "ma20_slope_pct": round(ma20_slope, 4),
            "deviation_ma20_pct": round(deviation_pct, 3),
        })
        reasons.append(self.cond("趋势缓慢抬升", trend_score >= 70.0, round(trend_score, 1),
                                 "趋势子分 ≥ 70", float(p["w_trend"])))
        reasons.append(self.cond("价 > MA20 > MA60", above and above_long,
                                 f"{close:.2f}/{ma20:.2f}/{ma60:.2f}", "多头排列", 0.0))
        reasons.append(self.cond("MA20 斜率向上", ma20_slope > 0, round(ma20_slope, 4),
                                 f"≥ {min_slope}%/日", 0.0))
        reasons.append(self.cond("20日涨幅温和", low_r <= change_20d <= high_r,
                                 round(change_20d, 2), f"{low_r}% ~ {high_r}%", 0.0))
        reasons.append(self.cond("未过度偏离MA20", deviation_pct <= 15.0,
                                 round(deviation_pct, 2), "≤ 15%", 0.0))

        # ---------------- 2) 量能分 ----------------
        vol_ratio = series.volume_ratio_5_20()
        turnover = series.turnover_rate
        v_low, v_high = float(p["vol_ratio_min"]), float(p["vol_ratio_max"])
        if vol_ratio <= 0:
            vol_part = 30.0
        elif v_low <= vol_ratio <= v_high:
            vol_part = 60.0
        elif vol_ratio < v_low:
            vol_part = 60.0 * (0.5 + 0.5 * (vol_ratio / max(v_low, 1e-6)))
        else:
            vol_part = 60.0 * max(0.0, 1.0 - (vol_ratio - v_high) / max(v_high, 1.0))
        t_low, t_high = float(p["turnover_min"]), float(p["turnover_max"])
        if turnover <= 0:
            turn_part = 20.0   # 盘前无换手 → 按中性偏保守处理
        elif t_low <= turnover <= t_high:
            turn_part = 40.0
        elif turnover < t_low:
            turn_part = 40.0 * (0.5 + 0.5 * (turnover / max(t_low, 1e-6)))
        else:
            turn_part = 40.0 * max(0.0, 1.0 - (turnover - t_high) / max(t_high, 1.0))
        volume_score = _clamp(vol_part + turn_part)

        metrics.update({"vol_ratio_5_20": round(vol_ratio, 3), "turnover_rate": round(turnover, 3)})
        reasons.append(self.cond("温和放量", v_low <= vol_ratio <= v_high, round(vol_ratio, 2),
                                 f"{v_low} ~ {v_high}", float(p["w_volume"]),
                                 "5日均量/20日均量"))
        reasons.append(self.cond("换手不过热", t_low <= turnover <= t_high, round(turnover, 2),
                                 f"{t_low}% ~ {t_high}%", 0.0))

        # ---------------- 3) 低波动分 ----------------
        atr_pct = (series.atr_value / close * 100.0) if close > 0 else 99.0
        max_gain = series.max_single_day_gain(int(p["single_day_window"]))
        drawdown = series.max_drawdown(20)
        atr_part = _linear(atr_pct, best=0.5, worst=max(float(p["atr_pct_max"]) * 1.5, 1.0)) * 0.4
        gain_limit = float(p["max_single_day_gain"])
        gain_part = 30.0 if max_gain < gain_limit else _clamp(30.0 * (1 - (max_gain - gain_limit) / 5.0))
        dd_limit = float(p["max_drawdown_20d"])
        dd_part = 30.0 if drawdown < dd_limit else _clamp(30.0 * (1 - (drawdown - dd_limit) / 10.0))
        low_vol_score = _clamp(atr_part + gain_part + dd_part)

        metrics.update({
            "atr_pct": round(atr_pct, 3),
            "max_single_day_gain": round(max_gain, 3),
            "max_drawdown_20d": round(drawdown, 3),
        })
        reasons.append(self.cond("波动率低", atr_pct <= p["atr_pct_max"], round(atr_pct, 2),
                                 f"≤ {p['atr_pct_max']}%", float(p["w_low_vol"]), "ATR14/收盘价"))
        reasons.append(self.cond("无明显涨停", max_gain < gain_limit, round(max_gain, 2),
                                 f"< {gain_limit}%", 0.0, f"近{int(p['single_day_window'])}日单日最大涨幅"))
        reasons.append(self.cond("回撤可控", drawdown < dd_limit, round(drawdown, 2),
                                 f"< {dd_limit}%", 0.0, "近20日"))

        # ---------------- 4) 相对强度分 ----------------
        benchmark = params.get("_benchmark_ret20") if isinstance(params, dict) else None
        if benchmark is None:
            rs_score = 50.0
            excess = None
        else:
            excess = change_20d - float(benchmark)
            rs_score = self._relative_strength_score(excess, float(p["excess_min"]), float(p["excess_max"]))
        metrics["excess_ret"] = round(excess, 3) if excess is not None else None
        reasons.append(self.cond(
            "跑赢沪深300且不过热", excess is not None and 0 <= excess <= float(p["excess_max"]),
            round(excess, 2) if excess is not None else None,
            f"0% ~ {p['excess_max']}%", float(p["w_relative_strength"]),
            "基准不可得时按中性 50 分处理",
        ))

        # ---------------- 5) 关注度分 ----------------
        attention_rank = params.get("_attention_rank") if isinstance(params, dict) else None
        attention_score = self._attention_score(
            attention_rank, amount, int(p["popularity_top_n"]), float(p["attention_default"])
        )
        metrics["attention_rank"] = attention_rank
        reasons.append(self.cond(
            "市场关注度不高", attention_score >= 40.0, attention_rank,
            f"人气排名 > {int(p['popularity_top_n'])} 或成交额代理", float(p["w_attention"]),
        ))

        # ---------------- 加权总分 ----------------
        total = (
            float(p["w_trend"]) * trend_score
            + float(p["w_volume"]) * volume_score
            + float(p["w_low_vol"]) * low_vol_score
            + float(p["w_relative_strength"]) * rs_score
            + float(p["w_attention"]) * attention_score
        )

        # ---------------- 板块共振加分 ----------------
        resonance_ratio = params.get("_sector_up_ratio") if isinstance(params, dict) else None
        bonus = 0.0
        if resonance_ratio is not None:
            threshold = max(float(p["resonance_member_ratio"]), 1e-6)
            factor = _clamp(float(resonance_ratio) / threshold, 0.0, 1.0)
            bonus = float(p["resonance_bonus_min"]) + (
                float(p["resonance_bonus_max"]) - float(p["resonance_bonus_min"])
            ) * factor
            if resonance_ratio < threshold:
                bonus = 0.0
            metrics["sector_resonance_ratio"] = round(float(resonance_ratio), 4)
        total = min(100.0, total + bonus)

        threshold = float(p["watchlist_threshold"])
        passed = total >= threshold and not prescreen_issues
        if prescreen_issues:
            reasons.insert(0, self.cond("基础预筛", False, "; ".join(prescreen_issues),
                                        "非ST/市值≥50亿/成交额≥1亿", 0.0))
        reasons.insert(0, self.cond(
            "是否入选", passed, round(total, 1),
            f"总分 ≥ {threshold} 且通过基础预筛", 0.0,
            f"总分 {total:.1f} / 门槛 {threshold}"
            + ("；存在预筛问题" if prescreen_issues else ""),
        ))

        return Signal(
            strategy=self.key, code=series.code, name=series.name,
            score=round(total, 2), passed=passed, reasons=reasons, metrics={
                **metrics,
                "trend_score": round(trend_score, 2),
                "volume_score": round(volume_score, 2),
                "low_vol_score": round(low_vol_score, 2),
                "relative_strength_score": round(rs_score, 2),
                "attention_score": round(attention_score, 2),
                "resonance_bonus": round(bonus, 2),
            },
            stop_loss=round(ma20 * 0.97, 3),
            entry_low=round(ma20 * 1.0, 3),
            entry_high=round(close * 1.02, 3),
            tags=["潜涨"] + (["板块共振"] if bonus > 0 else []),
            note="；".join(prescreen_issues) if prescreen_issues else "",
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _relative_strength_score(excess: float, low: float, high: float) -> float:
        """超额收益 0% → 100 分, 越高分越低(超额过大说明已经涨过头, 不再是"悄悄")。"""
        if excess < 0:
            return _clamp(100.0 * (1 + excess / 10.0))
        if excess <= high:
            return _clamp(100.0 - 20.0 * (excess / max(high, 1e-6)))
        return _clamp(max(0.0, 80.0 - (excess - high) * 3.0))

    @staticmethod
    def _attention_score(rank: Any, amount: float, top_n: int, default: float) -> float:
        if rank is None:
            # 无排名数据时用成交额做代理, 并与默认分混合(避免代理指标主导)
            if amount <= 0:
                return default
            proxy = _linear(amount, best=5e7, worst=3e9)
            return 0.6 * proxy + 0.4 * default
        try:
            rank_value = int(rank)
        except (TypeError, ValueError):
            return default
        if rank_value <= top_n:
            return _clamp(40.0 * (1.0 - (top_n - rank_value) / top_n))
        return 90.0

    def rule_conditions(self, params: Mapping[str, Any]) -> list[Condition]:
        p = self.merged_params(params)
        return [
            self.cond("价 > MA20 > MA60", True, None, "-", 0.0),
            self.cond("MA20 斜率向上", True, None, f"≥ {p['ma20_slope_min_pct']}%/日", 0.0),
            self.cond("20日涨幅温和", True, None, f"{p['change_20d_min']}% ~ {p['change_20d_max']}%", 0.30),
            self.cond("温和放量", True, None, f"量比 {p['vol_ratio_min']} ~ {p['vol_ratio_max']}", 0.20),
            self.cond("波动率低", True, None, f"ATR/价格 ≤ {p['atr_pct_max']}%", 0.20),
            self.cond("跑赢沪深300且不过热", True, None,
                      f"超额 0% ~ {p['excess_max']}%", 0.15),
            self.cond("市场关注度不高", True, None, f"人气排名 > {p['popularity_top_n']}", 0.15),
        ]

    def backtest_rules(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "stop_loss_pct": 8.0, "take_profit_pct": 0.0,
            "max_hold_days": 20, "break_ma": 20, "use_atr_stop": False,
        }


__all__ = ["QuietRiseStrategy"]
