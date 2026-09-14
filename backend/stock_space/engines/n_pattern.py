"""N 字战法选股 —— 源自 NPatternStrategy。

形态定义: **倍量点火阳线 → 缩量回调 2~6 天(不破点火低点) → 二波启动**。

两个买点:
  * **B1 回踩企稳低吸** —— 回调末端出现企稳迹象(小实体、缩量、贴近 MA10/MA20、RSI 中性);
  * **B2 放量突破确认** —— 放量突破回调期间最高点。

大盘闸门(环境风控大锁)是这套战法最关键的一环 —— 原文回测显示它在熊市胜率仅
37.5%, 因此本引擎默认带 ``env_gate``: 指数 20 日跌幅 < -3% 时直接不开新仓。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from .base import Condition, Series, Signal, Strategy, register
from .indicators import rsi, safe_float, slope, sma


@register
class NPatternStrategy(Strategy):
    key = "n_pattern"
    name = "N字战法"
    category = "pattern"
    description = (
        "倍量点火阳线 → 缩量回调 2~6 天且不破点火低点 → 二波启动。"
        "B1 为回踩企稳低吸, B2 为放量突破回调高点确认; 带大盘环境闸门(指数走弱时停开新仓)。"
    )
    source = "NPatternStrategy《N字战法.md》+ engine/strategy.py"
    regime = "只在大盘 MA20 向上或横盘时使用; 主跌/退潮期必须空仓(原文胜率仅 37.5%)。"
    min_bars = 130

    PARAMS: dict[str, Any] = {
        # 环境闸门
        "env_gate_enabled": True,
        "index_20d_min_pct": -3.0,
        "index_ma20_slope_min": 0.05,
        # 点火
        "ignite_min_pct": 7.0,
        "ignite_vol_ratio": 2.0,
        "ignite_vol_ma_days": 5,
        "ignite_close_near_high": 0.35,   # (high-close)/(high-low) 上限
        # 回调
        "pullback_min_days": 2,
        "pullback_max_days": 6,
        "low_breach_tol": 0.99,
        "pullback_day_max_abs_pct": 5.0,
        "avg_vol_ratio_max": 0.5,
        "depth_max": 0.5,
        "max_lookback": 12,               # 点火日必须在最近 N 根内
        # 个股均线
        "ma20_slope_min": 0.0,
        "ma20_required": True,
        # B1
        "b1_max_abs_pct": 2.2,
        "b1_vol_ratio_max": 0.8,
        "b1_ma_band_pct": 3.5,
        "b1_rsi_min": 30.0,
        "b1_rsi_max": 70.0,
        # B2
        "b2_min_pct": 5.0,
        "b2_vol_ratio": 1.2,
        "b2_rsi_max": 80.0,
        "b2_chase_guard_pct": 8.0,
        # 风控
        "stop_hard_pct": 8.0,
        "stop_low_discount": 0.99,
        "take_profit_pct": 25.0,
        "trail_after_pct": 5.0,
        "trail_drawdown_pct": 15.0,
        "max_hold_days": 12,
        "min_score": 4.0,
    }
    PARAM_HINTS = {
        "ignite_min_pct": ("点火阳线涨幅下界", "%"),
        "ignite_vol_ratio": ("点火量比下界", ""),
        "pullback_min_days": ("回调天数下界", "日"),
        "pullback_max_days": ("回调天数上界", "日"),
        "stop_hard_pct": ("硬止损", "%"),
        "take_profit_pct": ("目标止盈", "%"),
        "index_20d_min_pct": ("大盘20日跌幅下限", "%"),
    }

    # ------------------------------------------------------------------ #
    def evaluate(self, series: Series, params: Mapping[str, Any]) -> Signal:
        p = self.merged_params(params)
        if series.length < self.min_bars:
            return Signal(
                strategy=self.key, code=series.code, name=series.name, score=0.0,
                passed=False, metrics={"bars": series.length},
                reasons=[self.cond("K线充足", False, series.length, f"≥{self.min_bars}", 0.0)],
            )

        conditions: list[Condition] = []
        metrics: dict[str, Any] = {}

        # ---------------- 大盘闸门 ----------------
        gate = params.get("_env_gate") if isinstance(params, dict) else None
        gate_mode = "full" if gate is None else str(gate)
        metrics["env_gate"] = gate_mode
        if p["env_gate_enabled"]:
            conditions.append(self.cond(
                "大盘环境闸门", gate_mode != "off",
                {"full": "可正常开仓", "half": "减半仓", "off": "停开新仓"}.get(gate_mode, gate_mode),
                f"指数20日跌幅 ≥ {p['index_20d_min_pct']}%", 0.0,
                "原文回测: 主跌期 N 字战法胜率仅 37.5%",
            ))
            if gate_mode == "off":
                return Signal(
                    strategy=self.key, code=series.code, name=series.name, score=0.0,
                    passed=False, reasons=conditions, metrics=metrics,
                    tags=["环境不合格"],
                    note="大盘处于主跌/退潮段, 本策略暂停开新仓",
                )

        # ---------------- 点火日 ----------------
        max_lookback = int(p["max_lookback"])
        ignite_index: int | None = None
        ignite_vol_ratio = 0.0
        for days_ago in range(int(p["pullback_min_days"]), min(max_lookback, series.length - 1) + 1):
            index = series.length - 1 - days_ago
            ratio = self._ignite_ok(series, index, p)
            if ratio is not None:
                ignite_index = index
                ignite_vol_ratio = ratio
                break

        if ignite_index is None:
            conditions.append(self.cond("倍量点火阳线", False, None,
                                        f"最近{max_lookback}日内无满足条件的点火日", 0.0))
            return Signal(
                strategy=self.key, code=series.code, name=series.name, score=0.0,
                passed=False, reasons=conditions, metrics=metrics,
            )

        pullback_days = series.length - 1 - ignite_index
        metrics.update({
            "ignite_date": series.dates[ignite_index],
            "pullback_days": pullback_days,
            "ignite_vol_ratio": round(ignite_vol_ratio, 2),
        })
        conditions.append(self.cond(
            "倍量点火阳线", True, round(ignite_vol_ratio, 2),
            f"涨幅 ≥ {p['ignite_min_pct']}% 且 量比 ≥ {p['ignite_vol_ratio']}", 0.15,
            f"点火日 {series.dates[ignite_index]}",
        ))

        # ---------------- 回调区间 ----------------
        zone_low = self._zone_low(series, ignite_index, p)
        conditions.append(self.cond(
            "缩量回调且不破点火低点", zone_low is not None,
            round(zone_low, 3) if zone_low is not None else None,
            f"回调 {p['pullback_min_days']}~{p['pullback_max_days']} 日, 均量 ≤ 点火日×{p['avg_vol_ratio_max']}",
            0.20,
        ))
        if zone_low is None:
            return Signal(
                strategy=self.key, code=series.code, name=series.name, score=0.0,
                passed=False, reasons=conditions, metrics=metrics,
            )
        metrics["zone_low"] = round(zone_low, 3)

        # ---------------- 个股均线 ----------------
        ma20 = series.ma(20)
        ma20_slope = slope(series.ma20[~np.isnan(series.ma20)][-6:], 5) if series.length >= 10 else 0.0
        ma_ok = bool(ma20 > 0 and series.close >= ma20 and ma20_slope >= float(p["ma20_slope_min"]))
        metrics["ma20_slope"] = round(ma20_slope, 4)
        conditions.append(self.cond(
            "均线多头(20/60上扬)", ma_ok, round(ma20_slope, 4),
            f"收盘 ≥ MA20 且 MA20 斜率 ≥ {p['ma20_slope_min']}%/日", 0.15,
        ))

        # ---------------- B1 / B2 ----------------
        b1_ok, b1_detail = self._check_b1(series, p)
        b2_ok, b2_detail = self._check_b2(series, ignite_index, p)
        metrics.update({"b1": b1_detail, "b2": b2_detail})

        conditions.append(self.cond("B1 回踩企稳低吸", b1_ok, None,
                                    f"横盘 ≤{p['b1_max_abs_pct']}%、缩量 ≤{p['b1_vol_ratio_max']}倍、"
                                    f"贴近MA10/20 ≤{p['b1_ma_band_pct']}%、RSI "
                                    f"{p['b1_rsi_min']}~{p['b1_rsi_max']}", 0.25, b1_detail.get("reason", "")))
        conditions.append(self.cond("B2 放量突破确认", b2_ok, None,
                                    f"收盘 > 回调高点、涨幅 ≥{p['b2_min_pct']}%、"
                                    f"量比 ≥{p['b2_vol_ratio']}", 0.25, b2_detail.get("reason", "")))

        # ---------------- 评分 ----------------
        score, signal_kind = self._score(series, ignite_index, zone_low, b1_ok, b2_ok, p, metrics)
        passed = (b1_ok or b2_ok) and ma_ok and gate_mode != "off" and score >= float(p["min_score"])

        # 追高保护
        if b2_ok:
            zone_high = self._zone_high(series, ignite_index)
            if zone_high and series.close > zone_high * (1 + float(p["b2_chase_guard_pct"]) / 100.0):
                passed = False
                conditions.append(self.cond("追高保护", False, round(series.close, 3),
                                            f"≤ 回调高点×{1 + p['b2_chase_guard_pct'] / 100:.2f}", 0.0))

        conditions.insert(0, self.cond(
            "是否入选", passed, round(score, 2),
            f"(B1 或 B2 命中) 且 均线多头 且 大盘闸门未关闭 且 评分 ≥ {p['min_score']}", 0.0,
            f"评分 {score:.2f} / 门槛 {p['min_score']}；B1={b1_ok} B2={b2_ok} "
            f"均线多头={ma_ok} 闸门={gate_mode}",
        ))

        # 止损: 硬止损 -8% 与形态止损(回调低点×0.99) 取较高者
        hard_stop = series.close * (1 - float(p["stop_hard_pct"]) / 100.0)
        pattern_stop = zone_low * float(p["stop_low_discount"])
        stop = max(hard_stop, pattern_stop)
        zone_high = self._zone_high(series, ignite_index) or series.close

        return Signal(
            strategy=self.key, code=series.code, name=series.name,
            score=round(score, 2), passed=passed, reasons=conditions, metrics=metrics,
            stop_loss=round(stop, 3),
            take_profit=round(series.close * (1 + float(p["take_profit_pct"]) / 100.0), 3),
            entry_low=round(series.close * 0.99, 3),
            entry_high=round(series.close * 1.01, 3),
            tags=["N字"] + ([signal_kind] if signal_kind else []),
            note=f"点火日 {series.dates[ignite_index]}, 回调 {pullback_days} 日",
        )

    # ------------------------------------------------------------------ #
    def _ignite_ok(self, series: Series, index: int, p: Mapping[str, Any]) -> float | None:
        """返回量比; 不满足点火条件返回 None。"""
        if index < 1:
            return None
        prev_close = safe_float(series.closes[index - 1])
        close = safe_float(series.closes[index])
        if prev_close <= 0:
            return None
        pct = (close / prev_close - 1.0) * 100.0
        if pct < float(p["ignite_min_pct"]):
            return None
        k = int(p["ignite_vol_ma_days"])
        if index - k < 0:
            return None
        avg_volume = float(np.mean(series.volumes[index - k:index]))
        ratio = safe_float(series.volumes[index]) / avg_volume if avg_volume > 0 else 0.0
        if ratio < float(p["ignite_vol_ratio"]):
            return None
        high, low = safe_float(series.highs[index]), safe_float(series.lows[index])
        rng = high - low
        if rng <= 0 or (high - close) / rng > float(p["ignite_close_near_high"]):
            return None
        return ratio

    def _zone_low(self, series: Series, ignite_index: int, p: Mapping[str, Any]) -> float | None:
        segment = range(ignite_index + 1, series.length)
        if not segment:
            return None
        lows = series.lows[ignite_index + 1:]
        closes = series.closes[ignite_index + 1:]
        volumes = series.volumes[ignite_index + 1:]
        if len(lows) == 0:
            return None
        zone_low = float(np.min(lows))
        base_low = safe_float(series.lows[ignite_index])
        if zone_low < base_low * float(p["low_breach_tol"]) - 1e-9:
            return None
        # 回调期任一日不能有大幅波动
        prev = series.closes[ignite_index:series.length - 1]
        with np.errstate(divide="ignore", invalid="ignore"):
            pcts = np.where(prev > 0, (closes / prev - 1.0) * 100.0, 0.0)
        if np.any(np.abs(pcts) > float(p["pullback_day_max_abs_pct"])):
            return None
        # 缩量
        ignite_volume = safe_float(series.volumes[ignite_index])
        if ignite_volume > 0 and float(np.mean(volumes)) > float(p["avg_vol_ratio_max"]) * ignite_volume:
            return None
        # 回调深度不超过第一波涨幅的 50%
        rally = max(safe_float(series.closes[ignite_index]) - safe_float(series.closes[ignite_index - 1]), 1e-9)
        depth = (safe_float(series.closes[ignite_index]) - zone_low) / rally
        if depth > float(p["depth_max"]) + 1e-9:
            return None
        return zone_low

    def _zone_high(self, series: Series, ignite_index: int) -> float | None:
        highs = series.highs[ignite_index + 1:]
        if len(highs) == 0:
            return None
        return float(np.max(highs))

    def _check_b1(self, series: Series, p: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {}
        prev_close = safe_float(series.closes[-2]) if series.length > 1 else series.close
        pct = (series.close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
        detail["pct"] = round(pct, 3)
        if abs(pct) > float(p["b1_max_abs_pct"]):
            detail["reason"] = f"当日涨跌幅 {pct:+.2f}% 超出企稳区间"
            return False, detail

        k = 5
        avg_volume = float(np.mean(series.volumes[-k - 1:-1])) if series.length > k else 0.0
        if avg_volume <= 0:
            detail["reason"] = "量能数据不足"
            return False, detail
        # 相对点火日的缩量由 _zone_low 保证, 这里比对近5日均量
        if series.volume > avg_volume * (1.0 / max(float(p["b1_vol_ratio_max"]), 1e-6)):
            detail["reason"] = f"当日量能 {series.volume / avg_volume:.2f} 倍于近5日均量, 缩量不足"
            return False, detail
        detail["vol_vs_ma5"] = round(series.volume / avg_volume, 3)

        ma10 = series.ma(10)
        ma20 = series.ma(20)
        band = float(p["b1_ma_band_pct"]) / 100.0
        near_ma = False
        if ma10 > 0 and abs(series.close - ma10) / ma10 <= band:
            near_ma = True
        if ma20 > 0 and abs(series.close - ma20) / ma20 <= band:
            near_ma = True
        detail["near_ma"] = near_ma
        if not near_ma:
            detail["reason"] = f"距 MA10/MA20 超过 {p['b1_ma_band_pct']}%, 尚未回踩到位"
            return False, detail

        rsi_values = rsi(series.closes, 14)
        rsi14 = safe_float(rsi_values[-1], 50.0)
        detail["rsi14"] = round(rsi14, 2)
        if not (float(p["b1_rsi_min"]) <= rsi14 <= float(p["b1_rsi_max"])):
            detail["reason"] = f"RSI14={rsi14:.1f} 不在 {p['b1_rsi_min']}~{p['b1_rsi_max']} 区间"
            return False, detail
        detail["reason"] = "回踩均线企稳, 缩量且 RSI 中性"
        return True, detail

    def _check_b2(self, series: Series, ignite_index: int, p: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {}
        zone_high = self._zone_high(series, ignite_index)
        if zone_high is None:
            detail["reason"] = "无回调区间"
            return False, detail
        if series.close <= zone_high:
            detail["reason"] = f"收盘 {series.close:.2f} 未突破回调高点 {zone_high:.2f}"
            return False, detail
        detail["zone_high"] = round(zone_high, 3)
        # 突破必须是"缩量整理确认后的突破", 即回调至少 2 天
        if series.length - 1 - ignite_index < int(p["pullback_min_days"]):
            detail["reason"] = "回调时间不足, 突破仓促"
            return False, detail

        prev_close = safe_float(series.closes[-2]) if series.length > 1 else series.close
        pct = (series.close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
        detail["pct"] = round(pct, 3)
        if pct < float(p["b2_min_pct"]):
            detail["reason"] = f"突破日涨幅 {pct:.2f}% < {p['b2_min_pct']}%"
            return False, detail

        k = 5
        avg_volume = float(np.mean(series.volumes[-k - 1:-1])) if series.length > k else 0.0
        ratio = series.volume / avg_volume if avg_volume > 0 else 0.0
        detail["vol_ratio"] = round(ratio, 3)
        if ratio < float(p["b2_vol_ratio"]):
            detail["reason"] = f"突破量比 {ratio:.2f} < {p['b2_vol_ratio']}"
            return False, detail

        rsi_values = rsi(series.closes, 14)
        rsi14 = safe_float(rsi_values[-1], 50.0)
        detail["rsi14"] = round(rsi14, 2)
        if rsi14 > float(p["b2_rsi_max"]):
            detail["reason"] = f"RSI14={rsi14:.1f} 过热"
            return False, detail
        detail["reason"] = "放量突破回调高点"
        return True, detail

    def _score(
        self, series: Series, ignite_index: int, zone_low: float,
        b1_ok: bool, b2_ok: bool, p: Mapping[str, Any], metrics: dict[str, Any],
    ) -> tuple[float, str]:
        """排序分(与原文一致的口径: 突破分 > 低吸分 > 观察分)。"""
        pullback_days = series.length - 1 - ignite_index
        avg_volume5 = float(np.mean(series.volumes[-6:-1])) if series.length > 6 else series.volume
        vol_ratio = series.volume / avg_volume5 if avg_volume5 > 0 else 0.0
        if b2_ok:
            ignite_close = safe_float(series.closes[ignite_index])
            prev_close = safe_float(series.closes[-2]) if series.length > 1 else series.close
            pct = (series.close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
            score = pct + vol_ratio * 3.0 + (7 - pullback_days) * 0.5
            return round(score, 2), "B2突破"
        if b1_ok:
            ignite_close = safe_float(series.closes[ignite_index])
            rally = max(ignite_close - safe_float(series.closes[ignite_index - 1]), 1e-9)
            depth = min(1.0, (ignite_close - zone_low) / rally)
            score = 12 - pullback_days + (0.5 - depth) * 4
            return round(score, 2), "B1回踩"
        return 4.0, "观察"

    def rule_conditions(self, params: Mapping[str, Any]) -> list[Condition]:
        p = self.merged_params(params)
        return [
            self.cond("大盘环境闸门", True, None, f"指数20日跌幅 ≥ {p['index_20d_min_pct']}%", 0.0),
            self.cond("倍量点火阳线", True, None,
                      f"涨幅 ≥{p['ignite_min_pct']}%、量比 ≥{p['ignite_vol_ratio']}、收盘贴近日内高点", 0.15),
            self.cond("缩量回调不破点火低点", True, None,
                      f"回调 {p['pullback_min_days']}~{p['pullback_max_days']} 日、"
                      f"均量 ≤点火日×{p['avg_vol_ratio_max']}、深度 ≤第一波涨幅×{p['depth_max']}", 0.20),
            self.cond("均线多头", True, None, "收盘≥MA20 且 MA20 上扬", 0.15),
            self.cond("B1 回踩企稳", True, None, f"横盘≤{p['b1_max_abs_pct']}%、贴近MA10/20、RSI中性", 0.25),
            self.cond("B2 放量突破", True, None, f"破回调高点、涨幅≥{p['b2_min_pct']}%、量比≥{p['b2_vol_ratio']}", 0.25),
        ]

    def backtest_rules(self, params: Mapping[str, Any]) -> dict[str, Any]:
        p = self.merged_params(params)
        return {
            "stop_loss_pct": p["stop_hard_pct"],
            "take_profit_pct": p["take_profit_pct"],
            "max_hold_days": p["max_hold_days"],
            "break_ma": 0,
            "use_atr_stop": False,
            "stop_at": "pattern_low",
            "trail_after_pct": p["trail_after_pct"],
            "trail_drawdown_pct": p["trail_drawdown_pct"],
        }


__all__ = ["NPatternStrategy"]
