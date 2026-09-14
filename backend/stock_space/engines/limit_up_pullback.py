"""涨停回调低吸 —— 源自 Limit-Up-Pullback-Buy-Setup，补充 NPatternStrategy 的严谨口径。

原始素材的核心观点: 「涨停是主力的出货舞台, 回调才是散户的低吸机会。」
本引擎只做一件事: **优质首板之后的缩量回调企稳低吸**, 赚第二波拉升的确定性利润。

定性说法 → 定量判据(逐条对应):

    只做低位首板, 不做连板妖股   → 前 1~2 日无涨停 且 120 日位置 ≤ 0.80
    只做放量实体涨停, 拒绝一字板 → 量比 ≥ 1.2 且 换手 3%~25% 且 排除一字板
    缩量回调是洗盘, 放量回调是出货 → 回调每日量 < 涨停日量 且 逐日递减(否则一票否决)
    守住关键支撑                 → 不破涨停日开盘价 × 0.995, 且站稳实体半分位
    小阳十字星收尾, 拒绝大阴线   → 末端实体 ≤ 1.5% 且重心不下移; 单日 ≤ -3% 视为破位
    板块情绪同步回暖             → 同行业成分股的 5 日涨幅 + 涨停占比 + 上涨家数
    五个信号必须共振, 缺一不买   → 五信号全部 passed 且总分 ≥ buy_score
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from ..core.util import limit_pct
from .base import Condition, Series, Signal, Strategy, register
from .indicators import safe_float, sma, to_array


@register
class LimitUpPullbackStrategy(Strategy):
    key = "limit_up_pullback"
    name = "涨停回调低吸"
    category = "pattern"
    description = (
        "在优质首板涨停后的缩量回调企稳区低吸, 博第二波拉升。"
        "五信号(缩量/支撑/企稳/筑底/板块情绪)必须共振, 任一硬性否决即放弃。"
    )
    source = "Limit-Up-Pullback-Buy-Setup + NPatternStrategy"
    regime = "适用于大盘不在主跌段、存在成规模首板效应的行情。"
    min_bars = 140

    PARAMS: dict[str, Any] = {
        # 信号权重(合计 1.0)
        "weight_volume_shrink": 0.25,
        "weight_support": 0.25,
        "weight_stabilize": 0.15,
        "weight_kline_bottom": 0.20,
        "weight_sector": 0.15,
        # 涨停识别
        "limit_tolerance": 0.002,        # 涨幅 ≥ 涨停幅度 - 容差 视为涨停
        "st_limit_min": 0.048,
        "st_limit_max": 0.052,
        # 首板质量
        "max_previous_limit_days": 2,    # 前 N 日内无涨停 → 首板
        "position_window": 120,
        "max_position_ratio": 0.80,
        "min_vol_ratio": 1.2,
        "min_turnover": 3.0,
        "max_turnover": 25.0,
        # 回调窗口
        "min_pullback_days": 3,
        "max_pullback_days": 15,
        "break_tolerance": 0.005,
        # 缩量
        "vol_shrink_target": 0.60,       # max_ratio ≤ 0.40 满分
        # 筑底
        "small_body_pct": 1.5,
        "big_bearish_pct": -3.0,
        # 板块(用当日同板块成分股涨幅做代理, 无需额外数据源)
        "sector_pass_score": 55.0,
        "sector_default_score": 60.0,
        # 结论
        "buy_score": 75.0,
        "watch_score": 55.0,
        # 计划
        "stop_discount": 0.99,
        "max_stop_from_close_pct": 12.0,  # 止损过远则不入池
    }
    PARAM_HINTS = {
        "min_pullback_days": ("回调天数下界", "日"),
        "max_pullback_days": ("回调天数上界", "日"),
        "min_vol_ratio": ("涨停日量比下界", ""),
        "min_turnover": ("涨停日换手下界", "%"),
        "max_position_ratio": ("120日位置上限", ""),
        "buy_score": ("买入评分门槛", "分"),
        "watch_score": ("观察评分门槛", "分"),
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

        closes = series.closes
        highs = series.highs
        lows = series.lows
        opens = series.opens
        volumes = series.volumes
        n = series.length

        limit_index = self._find_limit_up(series, p)
        if limit_index is None:
            return Signal(
                strategy=self.key, code=series.code, name=series.name, score=0.0,
                passed=False, metrics={"bars": n},
                reasons=[self.cond("近期存在涨停", False, None,
                                   f"最近 {int(p['max_pullback_days'])} 日内", 0.0,
                                   "没有涨停就没有回调低吸的对象")],
            )

        pullback_days = n - 1 - limit_index
        limit_type, type_note = self._classify_limit_up(series, limit_index, p)

        metric: dict[str, Any] = {
            "limit_date": series.dates[limit_index],
            "limit_days_ago": pullback_days,
            "limit_type": limit_type,
        }

        hard_rejects: list[str] = []
        if limit_type != "QUALITY":
            hard_rejects.append(type_note or f"涨停类型为 {limit_type}, 不符合优质首板标准")
        if pullback_days < int(p["min_pullback_days"]):
            hard_rejects.append(f"涨停后仅回调 {pullback_days} 日, 洗盘不充分")
        elif pullback_days > int(p["max_pullback_days"]):
            hard_rejects.append(f"涨停后已过 {pullback_days} 日, 超出观察窗口")

        signals: list[tuple[str, float, bool, dict[str, Any], float]] = []
        signals.append(self._signal_volume_shrink(series, limit_index, p))
        signals.append(self._signal_support(series, limit_index, p))
        signals.append(self._signal_stabilize(series, limit_index, p))
        signals.append(self._signal_kline_bottom(series, limit_index, p))
        signals.append(self._signal_sector(series, p))

        weights = {
            "volume_shrink": float(p["weight_volume_shrink"]),
            "support": float(p["weight_support"]),
            "stabilize": float(p["weight_stabilize"]),
            "kline_bottom": float(p["weight_kline_bottom"]),
            "sector": float(p["weight_sector"]),
        }
        labels = {
            "volume_shrink": "信号一·缩量回调", "support": "信号二·守住支撑",
            "stabilize": "信号三·企稳形态", "kline_bottom": "信号四·K线筑底",
            "sector": "信号五·板块情绪",
        }

        total = 0.0
        all_passed = True
        conditions: list[Condition] = []
        for key, score, ok, detail, _ in signals:
            weight = weights.get(key, 0.0)
            total += round(score / 100.0 * weight * 100.0, 2)
            all_passed = all_passed and ok
            metric.update({f"{key}_score": round(score, 2), f"{key}_passed": ok, **detail})
            conditions.append(self.cond(labels.get(key, key), ok, round(score, 1),
                                        "≥ 60 且满足判定", weight))

        # 硬性否决
        vol_detail = next((d for k, _, _, d, _ in signals if k == "volume_shrink"), {})
        sup_detail = next((d for k, _, _, d, _ in signals if k == "support"), {})
        kb_detail = next((d for k, _, _, d, _ in signals if k == "kline_bottom"), {})
        if not vol_detail.get("passed", False) and safe_float(vol_detail.get("max_vol_ratio")) >= 1.0:
            hard_rejects.append("回调放量超过涨停日量能, 主力出逃、筹码崩坏")
        if bool(sup_detail.get("effective_break")):
            hard_rejects.append("回调有效跌破涨停日开盘价, 形态破坏")
        if int(kb_detail.get("big_bearish_count") or 0) > 0:
            hard_rejects.append(f"回调末端出现 {int(kb_detail.get('big_bearish_count'))} 根单日跌幅超 3% 的大阴线")

        total = round(total, 1)
        score = 0.0 if hard_rejects else total
        passed = (not hard_rejects) and total >= float(p["buy_score"]) and all_passed

        # 交易计划
        limit_open = safe_float(opens[limit_index])
        limit_close = safe_float(closes[limit_index])
        limit_high = safe_float(highs[limit_index])
        close = series.close
        half = (limit_open + limit_close) / 2.0
        stop = round(limit_open * float(p["stop_discount"]), 3)
        stop_distance = (1 - stop / close) * 100.0 if close > 0 else 100.0
        if stop_distance > float(p["max_stop_from_close_pct"]):
            metric["stop_distance_pct"] = round(stop_distance, 2)
            if passed:
                hard_rejects.append(f"止损位距离现价 {stop_distance:.1f}%, 风险收益比不划算")
                passed = False

        support_candidates = [v for v in (limit_open, series.ma(5), series.ma(10)) if 0 < v <= close]
        active_support = max(support_candidates) if support_candidates else min(
            [v for v in (limit_open, series.ma(5), series.ma(10)) if v > 0] or [close]
        )

        conditions.insert(0, self.cond(
            "是否入选", passed, total,
            f"五信号全部通过 且 总分 ≥ {p['buy_score']} 且无硬性否决", 0.0,
            f"总分 {total} / 门槛 {p['buy_score']}；五信号全通过={all_passed}；"
            f"否决={('；'.join(hard_rejects) if hard_rejects else '无')}",
        ))
        conditions.insert(1, self.cond(
            "优质首板", limit_type == "QUALITY", limit_type,
            "非ST/非连板/非高位/非一字/非尾盘偷袭/非弱封", 0.0, type_note,
        ))
        conditions.insert(2, self.cond(
            "回调窗口", int(p["min_pullback_days"]) <= pullback_days <= int(p["max_pullback_days"]),
            pullback_days, f"{int(p['min_pullback_days'])} ~ {int(p['max_pullback_days'])} 日", 0.0,
        ))
        conditions.append(self.cond("止损空间合理", stop_distance <= float(p["max_stop_from_close_pct"]),
                                    round(stop_distance, 2), f"≤ {p['max_stop_from_close_pct']}%", 0.0))

        return Signal(
            strategy=self.key, code=series.code, name=series.name,
            score=score, passed=passed, reasons=conditions, metrics=metric,
            stop_loss=stop,
            take_profit=round(max(limit_high, limit_close), 3),
            entry_low=round(active_support * 1.005, 3),
            entry_high=round(min(half, close), 3),
            tags=["涨停回调"] + ([f"{pullback_days}日回调"] if pullback_days else []),
            note="；".join(hard_rejects),
        )

    # ------------------------------------------------------------------ #
    # 涨停识别
    # ------------------------------------------------------------------ #
    def _is_limit_up(self, series: Series, index: int, p: Mapping[str, Any]) -> bool:
        if index < 1:
            return False
        prev_close = safe_float(series.closes[index - 1])
        close = safe_float(series.closes[index])
        if prev_close <= 0:
            return False
        pct = (close / prev_close - 1.0)
        name = series.name
        if "ST" in (name or "").upper():
            return float(p["st_limit_min"]) <= pct <= float(p["st_limit_max"])
        threshold = limit_pct(series.code, name) / 100.0
        return pct >= threshold - float(p["limit_tolerance"])

    def _find_limit_up(self, series: Series, p: Mapping[str, Any]) -> int | None:
        """在回调窗口内找到最近一个涨停日。"""
        n = series.length
        min_days = int(p["min_pullback_days"])
        max_days = int(p["max_pullback_days"])
        for days_ago in range(min_days, max_days + 1):
            index = n - 1 - days_ago
            if index < 1:
                break
            if self._is_limit_up(series, index, p):
                return index
        # 放宽: 允许 1 日回调(仅用于提示"洗盘不充分")
        for days_ago in range(1, max(min_days, 2)):
            index = n - 1 - days_ago
            if index >= 1 and self._is_limit_up(series, index, p):
                return index
        return None

    def _classify_limit_up(self, series: Series, index: int, p: Mapping[str, Any]) -> tuple[str, str]:
        """涨停质量八分类: 自上而下命中即返回, 只有 QUALITY 可交易。"""
        close = safe_float(series.closes[index])
        open_price = safe_float(series.opens[index])
        high = safe_float(series.highs[index])
        low = safe_float(series.lows[index])
        prev_close = safe_float(series.closes[index - 1])
        volume = safe_float(series.volumes[index])
        name = series.name or ""
        if "ST" in name.upper():
            return "ST_LIMIT", "ST 标的涨停, 不参与"

        # 连板
        for back in range(1, int(p["max_previous_limit_days"]) + 1):
            if index - back >= 1 and self._is_limit_up(series, index - back, p):
                return "CONSECUTIVE", f"前 {back} 日已有涨停, 属连板而非首板"

        # 高位
        window = int(p["position_window"])
        start = max(0, index - window)
        position = 0.0
        if index - start >= 20:
            segment_high = float(np.max(series.highs[start:index]))
            segment_low = float(np.min(series.lows[start:index]))
            if segment_high > segment_low:
                position = (close - segment_low) / (segment_high - segment_low)
        if position > float(p["max_position_ratio"]):
            return "HIGH_POSITION", f"处于{window}日高位区间({position:.2f}), 追高风险大"

        # 量比与换手
        vol_ma5 = safe_float(np.mean(series.volumes[max(0, index - 5):index])) if index >= 1 else 0.0
        vol_ratio = (volume / vol_ma5) if vol_ma5 > 0 else 0.0
        turnover = safe_float(series.quote.turnover_rate)
        threshold = limit_pct(series.code, name) / 100.0

        # 一字板
        if abs(open_price - high) < 1e-6 and abs(close - high) < 1e-6:
            if ((open_price / prev_close - 1.0) >= threshold * 0.95) or (vol_ratio and vol_ratio < float(p["min_vol_ratio"])):
                return "ONE_WORD", "一字板/缩量涨停, 无法低吸"
        # 尾盘偷袭
        if abs(close - high) < 1e-6 and prev_close > 0:
            intraday = (close - open_price) / prev_close
            if intraday >= 0.06 and turnover and turnover < 3.0:
                return "TAIL_SNEAK", "尾盘偷袭涨停, 封板质量差"
        # 弱封
        if high > 0 and close < high:
            retrace = (high - close) / prev_close if prev_close > 0 else 0.0
            if retrace >= 0.03:
                return "WEAK_SEAL", f"涨停未封住, 回落 {retrace * 100:.1f}%"

        if vol_ratio and vol_ratio < float(p["min_vol_ratio"]):
            return "WEAK_SEAL", f"涨停日量比 {vol_ratio:.2f} 偏低, 缺乏资金抢筹"
        if turnover and (turnover < float(p["min_turnover"]) or turnover > float(p["max_turnover"])):
            return "WEAK_SEAL", f"涨停日换手 {turnover:.1f}% 不在 {p['min_turnover']}%~{p['max_turnover']}% 区间"

        return "QUALITY", "放量实体首板, 形态合格"

    # ------------------------------------------------------------------ #
    # 五个信号
    # ------------------------------------------------------------------ #
    def _signal_volume_shrink(
        self, series: Series, limit_index: int, p: Mapping[str, Any]
    ) -> tuple[str, float, bool, dict[str, Any], float]:
        volumes = series.volumes
        limit_volume = safe_float(volumes[limit_index])
        segment = volumes[limit_index + 1:]
        detail: dict[str, Any] = {}
        if limit_volume <= 0 or len(segment) == 0:
            return "volume_shrink", 30.0, False, {"max_vol_ratio": 0.0}, 0.0
        ratios = segment / limit_volume
        max_ratio = float(np.max(ratios))
        monotonic = bool(np.all(np.diff(segment) < 0)) if len(segment) >= 2 else True
        target = max(1e-6, float(p["vol_shrink_target"]))
        score = 100.0 * max(0.0, min(1.0, (1.0 - max_ratio) / target))
        if not monotonic:
            score *= 0.75
        passed = monotonic and max_ratio < 1.0
        detail.update({
            "max_vol_ratio": round(max_ratio, 4),
            "volume_monotonic": monotonic,
            "passed": passed,
        })
        return "volume_shrink", score, passed, detail, 0.0

    def _signal_support(
        self, series: Series, limit_index: int, p: Mapping[str, Any]
    ) -> tuple[str, float, bool, dict[str, Any], float]:
        limit_open = safe_float(series.opens[limit_index])
        limit_close = safe_float(series.closes[limit_index])
        tolerance = float(p["break_tolerance"])
        break_level = limit_open * (1 - tolerance)
        lows_after = series.lows[limit_index + 1:]
        min_low = float(np.min(lows_after)) if len(lows_after) else limit_open
        close = series.close
        broken = bool(min_low < break_level or close < break_level)
        half = (limit_open + limit_close) / 2.0
        detail: dict[str, Any] = {
            "break_level": round(break_level, 3),
            "min_low_after": round(min_low, 3),
            "effective_break": broken,
        }
        if broken:
            return "support", 15.0, False, {**detail, "passed": False}, 0.0
        if close >= half:
            return "support", 100.0, True, {**detail, "passed": True}, 0.0
        span = max(half - limit_open, 1e-6)
        ratio = max(0.0, min(1.0, (close - limit_open) / span))
        score = max(25.0, min(70.0, 70.0 * ratio))
        return "support", score, score >= 60.0, {**detail, "passed": score >= 60.0}, 0.0

    def _signal_stabilize(
        self, series: Series, limit_index: int, p: Mapping[str, Any]
    ) -> tuple[str, float, bool, dict[str, Any], float]:
        lows = series.lows[limit_index + 1:]
        closes = series.closes[limit_index + 1:]
        highs = series.highs[limit_index + 1:]
        detail: dict[str, Any] = {}
        if len(lows) < 3:
            return "stabilize", 35.0, False, {"passed": False, "reason": "回调不足3日"}, 0.0
        rising = 0
        if lows[-1] >= lows[-2] * 0.998:
            rising += 1
        if lows[-2] >= lows[-3] * 0.998:
            rising += 1
        slope_first = _seg_slope(closes[: max(2, len(closes) // 2)])
        slope_last = _seg_slope(closes[-max(2, len(closes) // 2):])
        slope_easing = abs(slope_last) <= abs(slope_first) + 0.002
        ranges = np.where(highs > lows, (closes - lows) / np.maximum(highs - lows, 1e-9), 0.5)
        recent = float(np.mean(ranges[-3:]))
        earlier = float(np.mean(ranges[: max(1, len(ranges) - 3)]))
        position_up = recent >= earlier - 0.02
        passed = rising >= 2 and (slope_easing or position_up)
        score = 45.0 * (rising / 2.0) + 30.0 * (1.0 if slope_easing else 0.0) + 25.0 * (1.0 if position_up else 0.0)
        detail.update({
            "rising_lows": rising, "slope_easing": slope_easing,
            "position_up": position_up, "passed": passed,
        })
        return "stabilize", min(100.0, score), passed, detail, 0.0

    def _signal_kline_bottom(
        self, series: Series, limit_index: int, p: Mapping[str, Any]
    ) -> tuple[str, float, bool, dict[str, Any], float]:
        bearish_limit = float(p["big_bearish_pct"])
        closes = series.closes[limit_index + 1:]
        opens = series.opens[limit_index + 1:]
        prev_closes = series.closes[limit_index:len(series.closes) - 1]
        detail: dict[str, Any] = {}
        if len(closes) == 0:
            return "kline_bottom", 0.0, False, {"big_bearish_count": 0, "passed": False}, 0.0

        with np.errstate(divide="ignore", invalid="ignore"):
            pcts = np.where(prev_closes > 0, (closes / prev_closes - 1.0) * 100.0, 0.0)
        big_bearish = int(np.sum(pcts <= bearish_limit))

        prev_close = safe_float(series.closes[-2]) if series.length > 1 else safe_float(closes[-1])
        body = abs(safe_float(closes[-1]) - safe_float(opens[-1]))
        small_body = (body / prev_close * 100.0) <= float(p["small_body_pct"]) if prev_close > 0 else False
        tail = closes[-3:] if len(closes) >= 3 else closes
        tail_non_falling = bool(np.all(np.diff(tail) >= -0.005 * np.maximum(np.abs(tail[:-1]), 1e-9))) if len(tail) >= 2 else True
        center_holding = bool(safe_float(closes[-1]) >= float(np.min(closes)) * 0.998)
        passed = big_bearish == 0 and small_body and (tail_non_falling or center_holding)
        score = 45.0 * (1.0 if small_body else 0.0) + 35.0 * (1.0 if big_bearish == 0 else 0.0) + 20.0 * (1.0 if (tail_non_falling or center_holding) else 0.0)
        detail.update({
            "big_bearish_count": big_bearish, "small_body": small_body,
            "tail_non_falling": tail_non_falling, "center_holding": center_holding,
            "passed": passed,
        })
        return "kline_bottom", min(100.0, score), passed, detail, 0.0

    def _signal_sector(
        self, series: Series, p: Mapping[str, Any]
    ) -> tuple[str, float, bool, dict[str, Any], float]:
        """板块情绪。默认使用当日同行业成分股涨幅(由扫描服务注入), 缺数据按中性分处理。"""
        context = p.get("_sector_context") if isinstance(p, dict) else None
        default_score = float(p["sector_default_score"])
        if not isinstance(context, dict):
            return "sector", default_score, True, {"passed": True, "source": "default"}, 0.0
        avg_pct5 = safe_float(context.get("avg_change_pct"))
        limit_ratio = safe_float(context.get("limit_up_ratio"))
        up_ratio = safe_float(context.get("up_ratio"))
        score = (
            50.0
            + max(-30.0, min(30.0, avg_pct5 * 4.2))
            + max(0.0, min(6.0, limit_ratio * 90.0))
            + max(-7.0, min(7.0, (up_ratio - 0.5) * 40.0))
        )
        score = max(0.0, min(98.0, score))
        passed = score >= float(p["sector_pass_score"])
        return "sector", score, passed, {
            "passed": passed, "source": "sector_context",
            "avg_change_pct": round(avg_pct5, 3),
            "limit_up_ratio": round(limit_ratio, 4),
            "up_ratio": round(up_ratio, 4),
        }, 0.0

    def rule_conditions(self, params: Mapping[str, Any]) -> list[Condition]:
        p = self.merged_params(params)
        return [
            self.cond("优质首板(非连板/非高位/非一字)", True, None, f"120日位置 ≤ {p['max_position_ratio']}", 0.0),
            self.cond("回调天数", True, None,
                      f"{p['min_pullback_days']} ~ {p['max_pullback_days']} 日", 0.0),
            self.cond("信号一·缩量回调", True, None, "逐日递减且最大量 < 涨停日量", 0.25),
            self.cond("信号二·守住支撑", True, None, "不破涨停开盘价×0.995", 0.25),
            self.cond("信号三·企稳形态", True, None, "低点抬高 ≥2 次", 0.15),
            self.cond("信号四·K线筑底", True, None, "小实体 + 无大阴线", 0.20),
            self.cond("信号五·板块情绪", True, None, f"板块分 ≥ {p['sector_pass_score']}", 0.15),
        ]

    def backtest_rules(self, params: Mapping[str, Any]) -> dict[str, Any]:
        p = self.merged_params(params)
        return {
            "stop_loss_pct": 8.0,
            "take_profit_pct": 25.0,
            "max_hold_days": 15,
            "break_ma": 0,
            "use_atr_stop": False,
            "stop_at": "pattern_low",
        }


def _seg_slope(values: np.ndarray) -> float:
    """分段的归一化斜率(%/日)。"""
    array = to_array(values)
    array = array[np.isfinite(array)]
    if len(array) < 2:
        return 0.0
    x = np.arange(len(array), dtype=np.float64)
    denominator = float(np.sum((x - x.mean()) ** 2))
    if denominator == 0:
        return 0.0
    beta = float(np.sum((x - x.mean()) * (array - array.mean())) / denominator)
    base = float(np.mean(array))
    return (beta / base) if base else 0.0


__all__ = ["LimitUpPullbackStrategy"]
