"""趋势趋势选股 —— 源自 TrendSniper 「自上而下 + 基本面 + 技术面」体系。

对应原文《趋势策略.md》:
  逻辑看行业, 底气看业绩; 形态看均线, 买卖看支撑; 顺势不动摇, 破位坚决跑。

落地为五个可量化维度:
  1. **赛道** —— 所在板块是否形成趋势(板块内 ≥N 只站上 MA60、板块均涨 ≥ 阈值);
  2. **基本面** —— ROE / 净利同比 / 营收同比(东财财报, 取不到时不加分也不扣分);
  3. **技术面** —— 价 > MA20 > MA60 > MA120 且 MA60 上行; 创 60/120 日新高;
     回调低点抬高; 上涨温和放量、回调缩量; 月涨幅区间; 换手适中;
  4. **相对强度** —— 跑赢沪深300; 大盘下跌日抗跌;
  5. **风控** —— 加权打分排序 + 板块上限 + 止损价 = max(MA20×98%, 近10日回调低点×98%, 买入价×(1-8%))。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..core.util import limit_pct
from .base import Condition, Series, Signal, Strategy, register
from .indicators import safe_float, slope


@register
class TrendStrategy(Strategy):
    key = "trend"
    name = "趋势狙击"
    category = "trend"
    description = (
        "自上而下找赛道 + 基本面筛选 + 技术面确认: 均线多头排列、创阶段新高、回调低点抬高、"
        "温和放量、跑赢大盘, 止损价取「MA20×98% / 近10日低点×98% / 买入价-8%」三者最大值。"
    )
    source = "TrendSniper《趋势策略.md》"
    regime = "适用于指数在 MA20 上方、板块轮动有序的多头或震荡偏强行情。"
    min_bars = 130

    PARAMS: dict[str, Any] = {
        # 技术面
        "ma_bull_required": True,        # 必须满足 价>MA20>MA60>MA120
        "ma60_rising_lookback": 5,       # MA60 上行回看天数
        "new_high_window": 60,           # "创阶段新高"窗口
        "month_ret_min": 5.0,            # 近20日涨幅下界 %
        "month_ret_max": 45.0,           # 近20日涨幅上界 %
        "turnover_min": 0.8,             # 换手率下界 %
        "turnover_max": 15.0,            # 换手率上界 %
        "vol_ratio_min": 0.8,            # 5日/20日均量 下界
        "vol_ratio_max": 2.5,            # 5日/20日均量 上界
        "pullback_lookback": 20,         # 回调低点抬高判定窗口
        "max_atr_pct": 6.0,              # ATR/收盘价 上限 %
        # 相对强度
        "benchmark_code": "000300",      # 沪深300
        "rs_min": 0.0,                   # 相对沪深300 的超额收益下界 %
        # 基本面(可选)
        "require_fundamental": False,    # 是否强制要求财报数据
        "roe_min": 8.0,
        "profit_yoy_min": 0.0,
        "revenue_yoy_min": -10.0,
        # 风控
        "stop_loss_pct": 8.0,
        "stop_ma_discount": 0.98,
        "stop_low_discount": 0.98,
        "max_hold_days": 30,
        "min_score": 60.0,               # 入选门槛
        "sector_max_share": 0.5,         # 单板块最多占比
        # 赛道
        "sector_min_members": 4,
        "sector_ma60_ratio": 0.30,       # 板块内站上 MA60 的比例
        "sector_avg_ret_min": 2.0,       # 板块20日均涨 %
    }
    PARAM_HINTS = {
        "month_ret_min": ("近20日涨幅下界", "%"),
        "month_ret_max": ("近20日涨幅上界", "%"),
        "new_high_window": ("阶段新高窗口", "日"),
        "turnover_min": ("换手率下界", "%"),
        "turnover_max": ("换手率上界", "%"),
        "max_atr_pct": ("ATR/价格上限", "%"),
        "stop_loss_pct": ("固定止损", "%"),
        "min_score": ("入选评分门槛", "分"),
        "sector_max_share": ("单板块占比上限", ""),
    }

    # ------------------------------------------------------------------ #
    def evaluate(self, series: Series, params: Mapping[str, Any]) -> Signal:
        p = self.merged_params(params)
        conditions: list[Condition] = []
        close = series.close
        metrics: dict[str, Any] = {}

        if series.length < self.min_bars:
            metrics["bars"] = series.length
            return Signal(
                strategy=self.key, code=series.code, name=series.name, score=0.0,
                passed=False, metrics=metrics,
                reasons=[self.cond("K线充足", False, series.length, f"≥{self.min_bars}", 0.0,
                                   "上市时间过短或数据不足")],
            )

        ma20, ma60, ma120 = series.ma(20), series.ma(60), series.ma(120)
        metrics.update({
            "close": round(close, 3), "ma20": round(ma20, 3),
            "ma60": round(ma60, 3), "ma120": round(ma120, 3),
        })

        # 1) 均线多头排列
        bull = bool(ma20 > 0 and ma60 > 0 and ma120 > 0 and close > ma20 > ma60 > ma120)
        conditions.append(self.cond(
            "均线多头排列", bull, round(close, 2),
            "价 > MA20 > MA60 > MA120", 0.20,
            f"MA20={ma20:.2f} MA60={ma60:.2f} MA120={ma120:.2f}",
        ))

        # 2) MA60 上行
        lookback = int(p["ma60_rising_lookback"])
        ma60_slope = slope(series.ma60[~np.isnan(series.ma60)][-max(3, lookback):], max(3, lookback))
        conditions.append(self.cond(
            "MA60 上行", ma60_slope > 0, round(ma60_slope, 4),
            "斜率 > 0 (%/日)", 0.12, f"MA60 近{lookback}日斜率 {ma60_slope:+.3f}%/日",
        ))

        # 3) 创阶段新高
        window = int(p["new_high_window"])
        recent_high = float(np.max(series.highs[-window:])) if series.length >= window else 0.0
        new_high = bool(recent_high > 0 and close >= recent_high * 0.995)
        conditions.append(self.cond(
            f"创{window}日新高", new_high, round(close, 2),
            f"≥ 区间高点×0.995({recent_high:.2f})", 0.13,
        ))

        # 4) 月涨幅区间
        month_ret = series.ret(20)
        metrics["month_ret"] = round(month_ret, 3)
        in_range = p["month_ret_min"] <= month_ret <= p["month_ret_max"]
        conditions.append(self.cond(
            "月涨幅适中", in_range, round(month_ret, 2),
            f"{p['month_ret_min']}% ~ {p['month_ret_max']}%", 0.10,
            "涨幅过小说明未启动, 过大说明已透支",
        ))

        # 5) 回调低点抬高
        pullback = int(p["pullback_lookback"])
        rising = self._pullback_rising(series, pullback)
        conditions.append(self.cond(
            "回调低点抬高", rising, None, f"近{pullback}日低点逐级抬高", 0.10,
        ))

        # 6) 量能配合
        vol_ratio = series.volume_ratio_5_20()
        metrics["vol_ratio_5_20"] = round(vol_ratio, 3)
        vol_ok = p["vol_ratio_min"] <= vol_ratio <= p["vol_ratio_max"]
        conditions.append(self.cond(
            "量能温和", vol_ok, round(vol_ratio, 2),
            f"{p['vol_ratio_min']} ~ {p['vol_ratio_max']}", 0.10,
            "5日均量/20日均量; 过大是冲高, 过小是没有资金关注",
        ))

        # 7) 换手适中
        turnover = series.turnover_rate
        turnover_ok = p["turnover_min"] <= turnover <= p["turnover_max"]
        conditions.append(self.cond(
            "换手适中", turnover_ok, round(turnover, 2),
            f"{p['turnover_min']}% ~ {p['turnover_max']}%", 0.06,
        ))

        # 8) 波动可控
        atr_pct = (series.atr_value / close * 100.0) if close > 0 else 99.0
        metrics["atr_pct"] = round(atr_pct, 3)
        conditions.append(self.cond(
            "波动可控", atr_pct <= p["max_atr_pct"], round(atr_pct, 2),
            f"≤ {p['max_atr_pct']}%", 0.07, "ATR/收盘价",
        ))

        # 9) 相对强度(需要基准; 拿不到时不扣分, 由上层决定)
        benchmark = params.get("_benchmark_ret20") if isinstance(params, dict) else None
        if benchmark is None:
            conditions.append(self.cond("跑赢沪深300", True, None, "基准不可得(中性处理)", 0.06))
        else:
            excess = month_ret - float(benchmark)
            metrics["excess_ret"] = round(excess, 3)
            conditions.append(self.cond(
                "跑赢沪深300", excess >= p["rs_min"], round(excess, 2),
                f"超额 ≥ {p['rs_min']}%", 0.06,
            ))

        # 10) 基本面(可选)
        finance = params.get("_finance") if isinstance(params, dict) else None
        if finance:
            roe = safe_float(finance.get("roe"))
            profit_yoy = safe_float(finance.get("profit_yoy"))
            revenue_yoy = safe_float(finance.get("revenue_yoy"))
            metrics.update({"roe": roe, "profit_yoy": profit_yoy, "revenue_yoy": revenue_yoy})
            fundamental_ok = (
                roe >= p["roe_min"]
                and profit_yoy >= p["profit_yoy_min"]
                and revenue_yoy >= p["revenue_yoy_min"]
            )
            conditions.append(self.cond(
                "基本面达标", fundamental_ok,
                f"ROE {roe:.1f}% / 净利 {profit_yoy:+.1f}% / 营收 {revenue_yoy:+.1f}%",
                "ROE、净利、营收同比三项下限", 0.06,
                f"报告期 {finance.get('report_date', '')}",
            ))
        elif p["require_fundamental"]:
            conditions.append(self.cond("基本面达标", False, None, "强制要求财报数据", 0.06,
                                        "未取到财报数据"))
        else:
            conditions.append(self.cond("基本面达标", True, None, "财报不可得(中性处理)", 0.06))

        score = self.score_from(conditions)
        passed = score >= p["min_score"] and bull
        # 把"最终是否入选"作为一条可见依据 —— 避免用户看到"条件大多通过却没入选"
        # 却找不到原因(入选判定 = 评分门槛 + 均线多头这两条硬约束)。
        conditions.insert(0, self.cond(
            "是否入选", passed, round(score, 1),
            f"评分 ≥ {p['min_score']} 且 均线多头", 0.0,
            f"评分 {score:.1f} / 门槛 {p['min_score']}；均线多头={bull}",
        ))

        # 止损价: 三个支撑位取最大(最保守的防守线里选最靠近的一个)
        lows = series.lows[-10:] if series.length >= 10 else series.lows
        recent_low = float(np.min(lows)) if len(lows) else close
        stop = max(
            ma20 * float(p["stop_ma_discount"]),
            recent_low * float(p["stop_low_discount"]),
            close * (1 - float(p["stop_loss_pct"]) / 100.0),
        )
        return Signal(
            strategy=self.key, code=series.code, name=series.name,
            score=score, passed=passed, reasons=conditions, metrics=metrics,
            stop_loss=round(stop, 3),
            entry_low=round(close * 0.99, 3),
            entry_high=round(close * 1.01, 3),
            tags=["趋势"] + (["创阶段新高"] if new_high else []) + (["多头排列"] if bull else []),
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _pullback_rising(series: Series, lookback: int) -> bool:
        """回调低点抬高: 把窗口切成两半, 后半段的最低点高于前半段最低点。"""
        if series.length < lookback + 2:
            return False
        segment = series.lows[-lookback:]
        half = len(segment) // 2
        if half < 2:
            return False
        first_low = float(np.min(segment[:half]))
        second_low = float(np.min(segment[half:]))
        return bool(second_low >= first_low * 0.995)

    def rule_conditions(self, params: Mapping[str, Any]) -> list[Condition]:
        return [
            self.cond("均线多头排列", True, None, "价 > MA20 > MA60 > MA120", 0.20),
            self.cond("MA60 上行", True, None, "斜率 > 0", 0.12),
            self.cond("创阶段新高", True, None, "收盘 ≥ 近60日高点×0.995", 0.13),
            self.cond("月涨幅适中", True, None, "20日涨幅 5%~45%", 0.10),
            self.cond("回调低点抬高", True, None, "后半段低点 ≥ 前半段低点×0.995", 0.10),
            self.cond("量能温和", True, None, "5日/20日均量 0.8~2.5", 0.10),
            self.cond("换手适中", True, None, "0.8%~15%", 0.06),
            self.cond("波动可控", True, None, "ATR/价格 ≤ 6%", 0.07),
            self.cond("跑赢沪深300", True, None, "20日超额 ≥ 0%", 0.06),
            self.cond("基本面达标", True, None, "ROE≥8%、净利同比≥0、营收同比≥-10%", 0.06),
        ]

    def backtest_rules(self, params: Mapping[str, Any]) -> dict[str, Any]:
        p = self.merged_params(params)
        return {
            "stop_loss_pct": p["stop_loss_pct"],
            "take_profit_pct": 0.0,
            "max_hold_days": p["max_hold_days"],
            "break_ma": 20,
            "use_atr_stop": False,
        }


__all__ = ["TrendStrategy"]
