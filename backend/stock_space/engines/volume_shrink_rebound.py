"""缩量回调后温和放量 —— 交易心法: 「缩量回调是洗盘, 放量下跌是出货」。

与 ``limit_up_pullback``（涨停回调低吸）的区别：那套**必须有涨停**作为起点，
本策略**不依赖涨停** —— 只要前期是强势股，回调期缩量、随后出现第一根温和放量阳线
即可。因此覆盖面更宽（次强势股也能入选），但换来的代价是必须更严格地约束
"放量" 的量级：温和放量是洗盘结束，爆量拉升往往是出货前的最后一冲。

定性说法 → 定量判据(逐条对应):

    强势股                → 收盘站上 MA60, 且 MA20 未破位(收盘 ≥ MA20×0.97)
    前期确有涨幅          → 近 ``trend_lookback`` 日区间涨幅 ≥ ``min_trend_gain``
    回调                  → 末端连续 ``min_pullback_days`` 日阴线(允许提前结束)
    缩量是洗盘            → 回调期均量 ≤ 均量的 ``shrink_ratio``(默认 70%),
                            且回调期**没有**单日量超过 ``dump_vol_ratio``(放量下跌=出货, 一票否决)
    跌幅收窄              → 最后一根阴线跌幅 < 前一根阴线跌幅, 且回调累计跌幅 ≤ ``max_pullback_pct``
    随后第一个温和放量阳线 → 当日阳线, 涨幅 ∈ [``min_volume_up_pct``, 涨停幅度),
                            量 ∈ [``mild_vol_min``, ``mild_vol_max``] × 回调期均量,
                            且收在当日振幅上半区
    守住支撑              → 回调最低点 ≥ MA20 × ``support_tolerance``

出场规则(风险管理模板, 由 ``backtest_rules`` 交给回测引擎):

    初始止损    2 倍 ATR(``use_atr_stop`` —— 引擎按建仓日 ATR 计算 2×ATR 的距离)
    止盈目标    +16.0%
    移动止盈    盈利 +8% 后启动回撤保护(引擎: 浮盈达阈值后自最高收盘回撤 6% 离场)
    破线离场    收盘跌破 MA20
    时间止损    12 个交易日
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..core.util import limit_pct
from .base import Condition, Series, Signal, Strategy, register
from .indicators import safe_float


@register
class VolumeShrinkReboundStrategy(Strategy):
    key = "volume_shrink_rebound"
    name = "缩量回调后温和放量"
    category = "pattern"
    description = (
        "强势股缩量回调洗盘后, 以第一根温和放量阳线作为洗盘结束的进场信号。"
        "核心纪律: 缩量回调是洗盘、放量下跌是出货 —— 回调期出现放量下跌直接一票否决。"
    )
    source = "用户自定义(交易心法: 缩量回调是洗盘, 放量下跌是出货)"
    regime = "适用于强势股(站上 MA60)在上升途中的正常回调; 主跌段效果差。"
    min_bars = 90

    PARAMS: dict[str, Any] = {
        # ---- 信号权重(合计 1.0) ----
        "weight_strong_trend": 0.15,     # 前期强势
        "weight_pullback": 0.15,         # 回调成立
        "weight_volume_shrink": 0.30,    # 缩量(心法的核心)
        "weight_stabilize": 0.15,        # 跌幅收窄/守住支撑
        "weight_rebound": 0.25,          # 温和放量阳线
        # ---- 强势股判定 ----
        "trend_lookback": 60,
        "min_trend_gain": 12.0,          # 近 60 日区间涨幅 %(现价相对区间低点)
        "ma_break_tolerance": 0.97,      # 收盘 ≥ MA20 × 该系数 视为未破位
        # ---- 回调识别 ----
        "min_pullback_days": 2,
        "max_pullback_days": 15,
        "max_pullback_pct": 18.0,        # 回调累计跌幅上限 %
        "pullback_vol_ma": 20,           # 比较基准: 20 日均量
        # ---- 缩量(核心) ----
        "shrink_ratio": 0.70,            # 回调期均量 / 均量 ≤ 70%
        "dump_vol_ratio": 1.50,          # 回调期单日量 > 1.5×均量 视为放量下跌(否决)
        # ---- 温和放量阳线 ----
        "mild_vol_min": 1.00,            # 放量倍数下界(相对回调期均量)
        "mild_vol_max": 2.20,            # 上界 —— 超过即"爆量", 疑似出货
        "min_volume_up_pct": 1.60,       # 当日涨幅下界 %
        "close_position_min": 0.50,      # 收盘在当日振幅中的位置下界
        "flat_limit_ratio": 0.80,        # 涨幅达涨停幅度的 80% 视为接近涨停
        # ---- 支撑 ----
        #: 回调收盘低点相对 MA20 的下限系数。
        #: 取 0.97 而不是 0.98 —— 洗盘常见"短暂击穿 MA20 再收回", 而**出场**规则
        #: 是"收盘跌破 MA20"再离场(引擎侧还有 ×0.985 缓冲), 入场比出场宽松一点
        #: 才自洽: 在均线附近接、真破了就走。0.98 会把正常洗盘全部否掉
        #: (实测一个 −5% 的三日回调, 收盘低点正好落在 MA20 的 0.974)。
        "support_tolerance": 0.97,
        # ---- 结论 ----
        "buy_score": 72.0,
        "watch_score": 55.0,
        # ---- 交易计划 ----
        "atr_multiple": 2.0,             # 初始止损 = 建仓价 - 2×ATR
        "take_profit_pct": 16.0,
        "trail_after_pct": 8.0,
        "break_ma": 20,
        "max_hold_days": 12,
        "max_stop_from_close_pct": 12.0, # 止损过远则不入池
    }
    PARAM_HINTS = {
        "shrink_ratio": ("回调缩量比例上限", "倍"),
        "dump_vol_ratio": ("放量下跌判定倍数", "倍"),
        "mild_vol_min": ("温和放量倍数下界", "倍"),
        "mild_vol_max": ("温和放量倍数上界", "倍"),
        "min_volume_up_pct": ("放量阳线涨幅下界", "%"),
        "min_trend_gain": ("前期区间涨幅下界", "%"),
        "max_pullback_pct": ("回调累计跌幅上限", "%"),
        "atr_multiple": ("初始止损 ATR 倍数", "倍"),
        "take_profit_pct": ("止盈目标", "%"),
        "trail_after_pct": ("启动移动止盈的浮盈", "%"),
        "max_hold_days": ("时间止损", "个交易日"),
        "buy_score": ("买入评分门槛", "分"),
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

        n = series.length
        close = series.close
        atr_value = series.atr_value
        ma20 = series.ma(20)
        vol_ma20 = safe_float(series.vol_ma20[-1]) if series.length else 0.0

        signals = [
            self._sig_strong_trend(series, p),
            self._sig_pullback(series, p),
            self._sig_volume_shrink(series, p),
            self._sig_stabilize(series, p),
            self._sig_rebound(series, p),
        ]
        weights = {
            "strong_trend": float(p["weight_strong_trend"]),
            "pullback": float(p["weight_pullback"]),
            "volume_shrink": float(p["weight_volume_shrink"]),
            "stabilize": float(p["weight_stabilize"]),
            "rebound": float(p["weight_rebound"]),
        }
        labels = {
            "strong_trend": "信号一·前期强势",
            "pullback": "信号二·回调成立",
            "volume_shrink": "信号三·缩量洗盘",
            "stabilize": "信号四·跌幅收窄",
            "rebound": "信号五·温和放量阳线",
        }

        total = 0.0
        all_passed = True
        conditions: list[Condition] = []
        metric: dict[str, Any] = {"bars": n}
        for key, score, ok, detail in signals:
            weight = weights.get(key, 0.0)
            total += round(score / 100.0 * weight * 100.0, 2)
            all_passed = all_passed and ok
            metric.update({f"{key}_score": round(score, 2), f"{key}_passed": ok, **detail})
            conditions.append(self.cond(labels.get(key, key), ok, round(score, 1),
                                        "≥ 60 且满足判定", weight))

        #: 硬性否决 —— 心法的红线，不参与加权，命中即出局
        hard_rejects: list[str] = []
        if bool(metric.get("dump_volume")):
            hard_rejects.append(
                f"回调期出现放量下跌(单日量 {metric.get('max_pullback_vol_ratio')}×均量) —— "
                "放量下跌是出货, 一票否决"
            )
        if bool(metric.get("near_limit_up")):
            hard_rejects.append(
                f"放量阳线涨幅 {metric.get('rebound_pct')}% 已接近涨停, 属追高而非温和放量"
            )
        if not bool(metric.get("support_held", True)):
            hard_rejects.append(
                f"回调最低 {metric.get('pullback_low')} 跌破 MA20×{p['support_tolerance']}"
            )
        if not bool(metric.get("pullback_found", False)):
            hard_rejects.append("当前不处于回调结构末端, 没有可介入的洗盘结束点")

        total = round(total, 1)
        score = 0.0 if hard_rejects else total
        passed = (not hard_rejects) and total >= float(p["buy_score"]) and all_passed

        # ---- 交易计划: 止损 2×ATR / 止盈 +16% ----
        stop = round(close - float(p["atr_multiple"]) * atr_value, 3) if atr_value > 0 else 0.0
        stop_distance = (1 - stop / close) * 100.0 if (close > 0 and stop > 0) else 100.0
        metric["atr"] = round(atr_value, 3)
        metric["stop_distance_pct"] = round(stop_distance, 2)
        if stop <= 0:
            hard_rejects.append("ATR 不可用, 无法按 2×ATR 设定初始止损")
            passed = False
        elif stop_distance > float(p["max_stop_from_close_pct"]):
            hard_rejects.append(
                f"2×ATR 止损距现价 {stop_distance:.1f}% 过远, 风险收益比不划算"
            )
            passed = False

        take = round(close * (1 + float(p["take_profit_pct"]) / 100.0), 3)
        conditions.insert(0, self.cond(
            "是否入选", passed, total,
            f"五信号全部通过 且 总分 ≥ {p['buy_score']} 且无硬性否决", 0.0,
            f"总分 {total} / 门槛 {p['buy_score']}；五信号全通过={all_passed}；"
            f"否决={('；'.join(hard_rejects) if hard_rejects else '无')}",
        ))
        conditions.append(self.cond(
            "止损空间合理", 0 < stop_distance <= float(p["max_stop_from_close_pct"]),
            round(stop_distance, 2), f"≤ {p['max_stop_from_close_pct']}% (2×ATR)", 0.0,
        ))

        return Signal(
            strategy=self.key, code=series.code, name=series.name,
            score=score, passed=passed, reasons=conditions, metrics=metric,
            stop_loss=stop, take_profit=take,
            entry_low=round(close * 0.995, 3), entry_high=round(close * 1.01, 3),
            tags=["缩量回调", "温和放量"]
            + ([f"回调{int(metric.get('pullback_days') or 0)}日"] if metric.get("pullback_days") else []),
            note="；".join(hard_rejects),
        )

    # ------------------------------------------------------------------ #
    # 信号一: 前期强势
    # ------------------------------------------------------------------ #
    def _sig_strong_trend(self, series: Series, p: Mapping[str, Any]):
        close = series.close
        ma60 = series.ma(60)
        ma20 = series.ma(20)
        lookback = max(20, int(p["trend_lookback"]))
        start = max(0, series.length - lookback)
        low = float(np.min(series.lows[start:])) if series.length > start else close
        gain = (close / low - 1.0) * 100.0 if low > 0 else 0.0
        above_ma60 = ma60 > 0 and close > ma60
        ma20_ok = ma20 > 0 and close >= ma20 * float(p["ma_break_tolerance"])
        target = float(p["min_trend_gain"])
        score = min(100.0, 50.0 * min(1.0, gain / target if target > 0 else 1.0)
                    + 25.0 * (1.0 if above_ma60 else 0.0)
                    + 25.0 * (1.0 if ma20_ok else 0.0))
        passed = above_ma60 and ma20_ok and gain >= target
        return ("strong_trend", score, passed, {
            "passed": passed, "trend_gain_pct": round(gain, 2),
            "above_ma60": above_ma60, "ma20_held": ma20_ok,
            "lookback_low": round(low, 3),
        })

    # ------------------------------------------------------------------ #
    # 信号二: 回调成立(找出回调起点)
    # ------------------------------------------------------------------ #
    def _sig_pullback(self, series: Series, p: Mapping[str, Any]):
        """从末端往前数连续阴线, 定位回调起点。

        回调的终点是"最后一根阴线", 起点是它之前的那根阳线 —— 复用了
        ``_locate_pullback`` 的结果, 避免各信号各自扫描出不一致的窗口。
        """
        closes = series.closes
        opens = series.opens
        meta = _locate_pullback(closes, opens, p)
        days = int(meta["days"])
        drop = meta["drop_pct"]
        min_days = int(p["min_pullback_days"])
        max_days = int(p["max_pullback_days"])
        max_drop = float(p["max_pullback_pct"])
        found = meta["found"]
        in_window = found and min_days <= days <= max_days
        pass_drop = drop <= max_drop
        score = 0.0
        if in_window:
            score += 60.0
            #: 回调幅度越温和越好(洗盘不深跌), 跌到上限的一半以内给满分
            score += 40.0 * max(0.0, min(1.0, (max_drop - drop) / max(max_drop / 2.0, 1e-6)))
        passed = in_window and pass_drop
        return ("pullback", min(100.0, score), passed, {
            "passed": passed, "pullback_found": found,
            "pullback_days": days, "pullback_drop_pct": round(drop, 2),
            "pullback_start_index": meta["start_index"],
            "pullback_end_index": meta["end_index"],
        })

    # ------------------------------------------------------------------ #
    # 信号三: 缩量洗盘(核心)
    # ------------------------------------------------------------------ #
    def _sig_volume_shrink(self, series: Series, p: Mapping[str, Any]):
        volumes = series.volumes
        closes = series.closes
        opens = series.opens
        meta = _locate_pullback(closes, opens, p)
        if not meta["found"]:
            return ("volume_shrink", 0.0, False, {"passed": False, "reason": "无回调段"})

        start, end = int(meta["start_index"]), int(meta["end_index"])
        window = volumes[start:end + 1]
        if len(window) == 0:
            return ("volume_shrink", 0.0, False, {"passed": False, "reason": "回调段为空"})

        base = _reference_volume(volumes, start, int(p["pullback_vol_ma"]))
        if base <= 0:
            return ("volume_shrink", 20.0, False, {"passed": False, "reason": "均量不可用"})

        mean_ratio = float(np.mean(window)) / base
        max_ratio = float(np.max(window)) / base
        shrink_target = float(p["shrink_ratio"])
        dump_ratio = float(p["dump_vol_ratio"])

        #: 缩量得分: 达到目标比例即满分, 越缩越好
        score = 100.0 * max(0.0, min(1.0, (1.0 - mean_ratio) / max(1e-6, 1.0 - shrink_target)))
        if max_ratio > dump_ratio:
            score = 0.0
        elif max_ratio > 1.0:
            score *= 0.6   #: 单日放量但没到出货程度, 打折
        passed = mean_ratio <= shrink_target and max_ratio <= dump_ratio
        return ("volume_shrink", min(100.0, score), passed, {
            "passed": passed,
            "pullback_vol_ratio": round(mean_ratio, 3),
            "max_pullback_vol_ratio": round(max_ratio, 3),
            "volume_base": round(base, 1),
            "dump_volume": bool(max_ratio > dump_ratio),
        })

    # ------------------------------------------------------------------ #
    # 信号四: 跌幅收窄 + 守住支撑
    # ------------------------------------------------------------------ #
    def _sig_stabilize(self, series: Series, p: Mapping[str, Any]):
        closes = series.closes
        opens = series.opens
        meta = _locate_pullback(closes, opens, p)
        if not meta["found"]:
            return ("stabilize", 0.0, False, {"passed": False, "reason": "无回调段"})

        start, end = int(meta["start_index"]), int(meta["end_index"])
        #: 逐日跌幅(回调段内)
        daily: list[float] = []
        for i in range(start, end + 1):
            prev = safe_float(closes[i - 1]) if i >= 1 else 0.0
            if prev > 0:
                daily.append((safe_float(closes[i]) / prev - 1.0) * 100.0)
        narrowing = False
        if len(daily) >= 2:
            narrowing = daily[-1] > daily[-2]        #: 最后一根跌幅小于前一根(注意都是负数)
        elif len(daily) == 1:
            narrowing = daily[-1] > -6.0             #: 只有一天时要求不是大阴线

        ma20 = series.ma(20)
        #: 用**收盘**低点而非盘中最低价 —— 与出场规则"收盘跌破 MA20"同一把尺子。
        #: 若用盘中最低，一根下影线就能否掉形态，而真实交易只按收盘价判定破线。
        pullback_low = float(np.min(closes[start:end + 1])) if end >= start else series.close
        support_level = ma20 * float(p["support_tolerance"]) if ma20 > 0 else 0.0
        support_held = bool(support_level <= 0 or pullback_low >= support_level)

        score = 60.0 * (1.0 if narrowing else 0.0) + 40.0 * (1.0 if support_held else 0.0)
        passed = narrowing and support_held
        return ("stabilize", score, passed, {
            "passed": passed, "narrowing": narrowing,
            "pullback_low": round(pullback_low, 3),
            "support_level": round(support_level, 3),
            "support_held": support_held,
            "last_two_daily_pct": [round(x, 2) for x in daily[-2:]],
        })

    # ------------------------------------------------------------------ #
    # 信号五: 温和放量阳线
    # ------------------------------------------------------------------ #
    def _sig_rebound(self, series: Series, p: Mapping[str, Any]):
        closes = series.closes
        opens = series.opens
        highs = series.highs
        lows = series.lows
        volumes = series.volumes
        meta = _locate_pullback(closes, opens, p)
        if not meta["found"]:
            return ("rebound", 0.0, False, {"passed": False, "reason": "无回调段"})

        start, end = int(meta["start_index"]), int(meta["end_index"])
        i = series.length - 1
        close = safe_float(closes[i])
        open_price = safe_float(opens[i])
        prev_close = safe_float(closes[i - 1]) if i >= 1 else close
        high = safe_float(highs[i])
        low = safe_float(lows[i])

        rebound_pct = (close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
        is_bullish = close > open_price

        #: 放量倍数以回调期均量为基准 —— 与"缩量"同一把尺子, 口径一致
        base = _reference_volume(volumes, start, int(p["pullback_vol_ma"]))
        window = volumes[start:end + 1] if end >= start else volumes[max(0, i - 3):i]
        base = float(np.mean(window)) if len(window) else base
        vol_ratio = (safe_float(volumes[i]) / base) if base > 0 else 0.0

        mild_min, mild_max = float(p["mild_vol_min"]), float(p["mild_vol_max"])
        vol_ok = mild_min <= vol_ratio <= mild_max
        up_ok = rebound_pct >= float(p["min_volume_up_pct"])
        #: 涨幅达到涨停幅度的 flat_limit_ratio 即视为接近涨停(追高), 一票否决
        limit_ratio = limit_pct(series.code, series.name) / 100.0
        near_limit = limit_ratio > 0 and (rebound_pct / 100.0) >= limit_ratio * float(p["flat_limit_ratio"])

        span = high - low
        close_pos = ((close - low) / span) if span > 0 else 0.5
        pos_ok = close_pos >= float(p["close_position_min"])

        score = 40.0 * (1.0 if up_ok else 0.0) + 40.0 * (1.0 if vol_ok else 0.0) \
            + 20.0 * (1.0 if pos_ok else 0.0)
        if not is_bullish:
            score = min(score, 30.0)
        passed = is_bullish and up_ok and vol_ok and pos_ok and not near_limit
        return ("rebound", score, passed, {
            "passed": passed, "bullish": is_bullish,
            "rebound_pct": round(rebound_pct, 2),
            "rebound_vol_ratio": round(vol_ratio, 3),
            "close_position": round(close_pos, 3),
            "near_limit_up": bool(near_limit),
        })

    # ------------------------------------------------------------------ #
    def rule_conditions(self, params: Mapping[str, Any]) -> list[Condition]:
        p = self.merged_params(params)
        return [
            self.cond("信号一·前期强势", True, None,
                      f"站上 MA60 且 {p['trend_lookback']} 日区间涨幅 ≥ {p['min_trend_gain']}%", 0.15),
            self.cond("信号二·回调成立", True, None,
                      f"回调 {p['min_pullback_days']}~{p['max_pullback_days']} 日 "
                      f"且累计跌幅 ≤ {p['max_pullback_pct']}%", 0.15),
            self.cond("信号三·缩量洗盘", True, None,
                      f"回调期均量 ≤ 均量×{p['shrink_ratio']}, "
                      f"且无单日量 > 均量×{p['dump_vol_ratio']}", 0.30),
            self.cond("信号四·跌幅收窄", True, None,
                      f"末根跌幅收窄 且 回调收盘低点 ≥ MA20×{p['support_tolerance']}", 0.15),
            self.cond("信号五·温和放量阳线", True, None,
                      f"阳线 且 量 ∈ [{p['mild_vol_min']}, {p['mild_vol_max']}]×回调均量 "
                      f"且 涨幅 ≥ {p['min_volume_up_pct']}%", 0.25),
            self.cond("风控模板", True, None,
                      f"止损 {p['atr_multiple']}×ATR / 止盈 +{p['take_profit_pct']}% / "
                      f"浮盈 +{p['trail_after_pct']}% 后回撤保护 / 破 MA{p['break_ma']} / "
                      f"最长 {p['max_hold_days']} 日", 0.0),
        ]

    def backtest_rules(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """风险管理模板: 2×ATR 止损 / +16% 止盈 / +8% 后移动止盈 / 破 MA20 / 12 日。

        注意 ``use_atr_stop`` 的语义由回测引擎实现(按建仓日 ATR 折算止损距离),
        这里只声明意图与倍数 —— 策略侧同时用它推导 ``Signal.stop_loss``,
        两者口径必须一致, 否则扫描显示的止损位会与回测成交价对不上。
        """
        p = self.merged_params(params)
        return {
            "stop_loss_pct": 0.0,                       # 交由 ATR 决定
            "use_atr_stop": True,
            "atr_multiple": float(p["atr_multiple"]),
            "take_profit_pct": float(p["take_profit_pct"]),
            "trail_after_pct": float(p["trail_after_pct"]),
            "break_ma": int(p["break_ma"]),
            "max_hold_days": int(p["max_hold_days"]),
        }


# --------------------------------------------------------------------------- #
# 回调段定位(供多个信号共用, 保证窗口一致)
# --------------------------------------------------------------------------- #
def _locate_pullback(
    closes: np.ndarray, opens: np.ndarray, p: Mapping[str, Any]
) -> dict[str, Any]:
    """从末端往前数连续阴线, 返回回调段。

    定义: 回调段 = [start, end], 其中 end 是**最后连续阴线的最后一根**,
    start 是这段阴线的第一根。当前 bar(``length-1``)若是放量阳线则不属于回调段,
    回调段在它之前结束 —— 这样"回调 → 阳线"的先后关系才成立。

    ``days`` 为回调段的交易日数; ``drop_pct`` 为回调期相对起点的跌幅(正数)。
    """
    n = len(closes)
    if n < 3:
        return {"found": False, "days": 0, "drop_pct": 0.0,
                "start_index": 0, "end_index": 0}

    #: 末端若已是阳线, 则从它的前一根开始找回调
    last = n - 1
    if safe_float(closes[last]) > safe_float(opens[last]):
        end = last - 1
    else:
        end = last

    if end < 1:
        return {"found": False, "days": 0, "drop_pct": 0.0,
                "start_index": 0, "end_index": 0}

    start = end
    while start >= 1 and safe_float(closes[start]) < safe_float(opens[start]):
        start -= 1
    #: 循环结束时 start 指向回调段之前的那根(阳线或越界), 回调段从 start+1 开始
    start += 1
    if start > end:
        return {"found": False, "days": 0, "drop_pct": 0.0,
                "start_index": 0, "end_index": 0}

    days = end - start + 1
    base_index = start - 1 if start >= 1 else start
    base_close = safe_float(closes[base_index])
    segment_low = float(np.min(closes[start:end + 1]))
    drop = (base_close - segment_low) / base_close * 100.0 if base_close > 0 else 0.0
    return {
        "found": True,
        "days": days,
        "drop_pct": max(0.0, drop),
        "start_index": start,
        "end_index": end,
        "base_close": base_close,
        "segment_low": segment_low,
    }


def _reference_volume(volumes: np.ndarray, before_index: int, window: int) -> float:
    """回调起点之前的均量 —— 作为"均量"的比较基准。

    取 ``before_index`` 之前 ``window`` 根(不含回调段), 避免把回调期的缩量
    算进基准导致"缩量"自我实现(基准被拉低 → 看起来没缩量)。
    """
    end = max(0, before_index)
    start = max(0, end - max(2, window))
    segment = volumes[start:end]
    segment = segment[np.isfinite(segment)]
    return float(np.mean(segment)) if len(segment) else 0.0


__all__ = ["VolumeShrinkReboundStrategy"]
