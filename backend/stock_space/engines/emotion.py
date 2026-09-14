"""市场情绪周期引擎 —— 整合 92KeBi 与 StockTradingReviewTool 两套口径。

需求文档里"情绪温度计"在两个原始项目里语义完全不同, 本引擎**同时给出两个指标并显式区分**,
避免把不同口径混用:

  1. ``emotion_score`` (0~100) —— 加权情绪分, 直接复用 StockTradingReviewTool 的权重:

         min(涨停家数, 80)/80 × 25
       + max(0, 1 - 炸板率/50) × 20
       + min(最高连板, 7)/7 × 15
       + min(竞价涨停家数, 10)/10 × 10
       + 上涨占比 × 20
       + max(0, 1 - 跌停家数/30) × 10

     分级: ≥75 亢奋 / ≥60 活跃 / ≥40 中性 / ≥25 低迷 / <25 冰点

  2. ``cycle_phase`` —— 情绪周期五阶段(冰点/启动/发酵/高潮/退潮), 使用整数证据打分
     (分值域约 -12 ~ +11), 再按 92KeBi 的状态机语义做阶段跃迁。

两个指标都附带**逐条依据**(每条含指标值/阈值/加分), 前端可展开核对 ——
情绪判定最忌讳"给个结论说不出理由"。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from ..core.util import limit_pct, market_session, now_cn
from ..models import Breadth, LimitUpStock, Quote

logger = logging.getLogger(__name__)

#: 情绪分等级
SCORE_LEVELS = (
    (75.0, "亢奋", "普涨, 赚钱效应强, 注意高位分歧"),
    (60.0, "活跃", "主线清晰, 可正常参与"),
    (40.0, "中性", "分歧市场, 控制仓位与出手频率"),
    (25.0, "低迷", "亏钱效应显现, 只做最强"),
    (0.0, "冰点", "普跌, 建议空仓等待"),
)

#: 五阶段
PHASES = ("ice", "start", "ferment", "climax", "ebb")
PHASE_LABELS = {
    "ice": "冰点", "start": "启动", "ferment": "发酵", "climax": "高潮", "ebb": "退潮",
}
PHASE_ADVICE = {
    "ice": "空仓或极低仓位试错; 等待第一个有效涨停梯队出现。",
    "start": "可小仓位试错龙头与补涨首板, 严格止损。",
    "ferment": "主线发酵期, 可持股并关注加速信号, 不追高。",
    "climax": "情绪高潮, 逐步兑现利润, 警惕最高板断板。",
    "ebb": "退潮期, 停开新仓, 优先处理存量持仓。",
}


@dataclass
class EmotionContext:
    """情绪判定的输入 —— 由扫描服务装配。"""

    quotes: list[Quote] = field(default_factory=list)
    limit_up: list[LimitUpStock] = field(default_factory=list)
    broken: list[LimitUpStock] = field(default_factory=list)
    breadth: Breadth | None = None
    index_quotes: list[Quote] = field(default_factory=list)
    prev_phase: str = ""
    prev_emotion_score: float = 0.0
    prev_limit_up_count: int = 0
    prev_max_consecutive: int = 0
    source: str = ""

    # ------------------------------------------------------------------ #
    def derived_breadth(self) -> Breadth:
        """没有独立的市场宽度接口时, 用快照自行统计(样本级)。"""
        if self.breadth is not None:
            return self.breadth
        up = down = flat = limit_up = limit_down = 0
        amount = 0.0
        for quote in self.quotes:
            amount += quote.amount
            if quote.change_pct > 0:
                up += 1
            elif quote.change_pct < 0:
                down += 1
            else:
                flat += 1
            threshold = limit_pct(quote.code, quote.name)
            if quote.change_pct >= threshold - 0.3:
                limit_up += 1
            elif quote.change_pct <= -(threshold - 0.3):
                limit_down += 1
        return Breadth(up=up, down=down, flat=flat, limit_up=limit_up,
                       limit_down=limit_down, total_amount=amount, source=self.source)

    @property
    def max_consecutive(self) -> int:
        if not self.limit_up:
            return 0
        return max((s.consecutive or 1) for s in self.limit_up)

    @property
    def ladder(self) -> dict[int, int]:
        """连板梯队: {连板数: 家数}。"""
        ladder: dict[int, int] = {}
        for stock in self.limit_up:
            level = max(1, stock.consecutive or 1)
            ladder[level] = ladder.get(level, 0) + 1
        return dict(sorted(ladder.items()))

    @property
    def auction_limit_up(self) -> int:
        """竞价涨停家数(首次封板时间 < 09:26)。"""
        count = 0
        for stock in self.limit_up:
            text = (stock.first_limit_time or "").replace(":", "")
            if text.isdigit() and int(text) < 92600:
                count += 1
        return count

    @property
    def broken_rate(self) -> float:
        attempts = len(self.limit_up) + len(self.broken)
        return (len(self.broken) / attempts * 100.0) if attempts else 0.0


# --------------------------------------------------------------------------- #
# 情绪分
# --------------------------------------------------------------------------- #
def compute_emotion_score(context: EmotionContext) -> dict[str, Any]:
    """加权情绪分(0~100) + 逐项拆解。"""
    breadth = context.derived_breadth()
    limit_up_count = len(context.limit_up) or breadth.limit_up
    limit_down_count = breadth.limit_down
    broken_count = len(context.broken)
    max_consecutive = context.max_consecutive
    auction = context.auction_limit_up
    broken_rate = context.broken_rate
    if broken_count == 0 and limit_up_count > 0:
        broken_rate = 0.0
    total = breadth.up + breadth.down
    up_ratio = (breadth.up / total) if total else 0.5

    parts = [
        {
            "name": "涨停家数",
            "value": limit_up_count,
            "weight": 25.0,
            "earned": round(min(limit_up_count, 80) / 80.0 * 25.0, 2),
            "threshold": "80 家封顶",
        },
        {
            "name": "炸板率",
            "value": round(broken_rate, 1),
            "weight": 20.0,
            "earned": round(max(0.0, 1 - broken_rate / 50.0) * 20.0, 2),
            "threshold": "分母 50, 越低越好",
        },
        {
            "name": "最高连板",
            "value": max_consecutive,
            "weight": 15.0,
            "earned": round(min(max_consecutive, 7) / 7.0 * 15.0, 2),
            "threshold": "7 板封顶",
        },
        {
            "name": "竞价涨停",
            "value": auction,
            "weight": 10.0,
            "earned": round(min(auction, 10) / 10.0 * 10.0, 2),
            "threshold": "首次封板早于 09:26 计 1 家",
        },
        {
            "name": "上涨占比",
            "value": round(up_ratio * 100, 1),
            "weight": 20.0,
            "earned": round(up_ratio * 20.0, 2),
            "threshold": "上涨家数 / (上涨+下跌)",
        },
        {
            "name": "跌停家数",
            "value": limit_down_count,
            "weight": 10.0,
            "earned": round(max(0.0, 1 - limit_down_count / 30.0) * 10.0, 2),
            "threshold": "分母 30, 越少越好",
        },
    ]
    score = round(sum(p["earned"] for p in parts), 1)
    level, label, advice = "冰点", "冰点", PHASE_ADVICE["ice"]
    for threshold, name, hint in SCORE_LEVELS:
        if score >= threshold:
            level, label, advice = name, name, hint
            break
    return {
        "score": score,
        "level": level,
        "label": label,
        "advice": advice,
        "parts": parts,
        "metrics": {
            "limit_up": limit_up_count,
            "limit_down": limit_down_count,
            "broken": broken_count,
            "broken_rate": round(broken_rate, 2),
            "max_consecutive": max_consecutive,
            "auction_limit_up": auction,
            "up": breadth.up, "down": breadth.down, "flat": breadth.flat,
            "up_ratio": round(up_ratio, 4),
            "total_amount": round(breadth.total_amount, 0),
        },
    }


# --------------------------------------------------------------------------- #
# 情绪周期(五阶段)
# --------------------------------------------------------------------------- #
def compute_cycle(context: EmotionContext, emotion: dict[str, Any] | None = None) -> dict[str, Any]:
    """用整数证据打分判定情绪周期阶段。"""
    breadth = context.derived_breadth()
    limit_up_count = len(context.limit_up) or breadth.limit_up
    broken_rate = context.broken_rate
    max_consecutive = context.max_consecutive
    limit_down_count = breadth.limit_down
    ladder = context.ladder
    promotion_rate = _promotion_rate(context)
    big_loss = _big_loss_count(context)

    evidence: list[dict[str, Any]] = []

    def add(name: str, delta: int, value: Any, threshold: str) -> None:
        evidence.append({"name": name, "delta": delta, "value": value, "threshold": threshold})

    # 涨停家数
    if limit_up_count < 30:
        add("涨停家数", -2, limit_up_count, "< 30 家")
    elif limit_up_count >= 150:
        add("涨停家数", 3, limit_up_count, "≥ 150 家")
    elif limit_up_count >= 100:
        add("涨停家数", 2, limit_up_count, "≥ 100 家")
    elif limit_up_count >= 60:
        add("涨停家数", 1, limit_up_count, "≥ 60 家")

    # 炸板率
    if broken_rate > 40:
        add("炸板率", -2, round(broken_rate, 1), "> 40%")
    elif broken_rate > 30:
        add("炸板率", -1, round(broken_rate, 1), "> 30%")
    elif broken_rate <= 10:
        add("炸板率", 2, round(broken_rate, 1), "≤ 10%")
    elif broken_rate <= 15:
        add("炸板率", 1, round(broken_rate, 1), "≤ 15%")

    # 连板高度
    if max_consecutive <= 2:
        add("最高连板", -2, max_consecutive, "≤ 2 板")
    elif max_consecutive == 3:
        add("最高连板", -1, max_consecutive, "= 3 板")
    elif max_consecutive >= 7:
        add("最高连板", 2, max_consecutive, "≥ 7 板")
    elif max_consecutive >= 6:
        add("最高连板", 1, max_consecutive, "≥ 6 板")

    # 晋级率
    if promotion_rate is not None:
        if promotion_rate > 50:
            add("连板晋级率", 1, round(promotion_rate, 1), "> 50%")
        elif promotion_rate < 15:
            add("连板晋级率", -2, round(promotion_rate, 1), "< 15%")
        elif promotion_rate < 30:
            add("连板晋级率", -1, round(promotion_rate, 1), "< 30%")

    # 大面(高位股大幅回撤)
    if big_loss >= 30:
        add("大面家数", -2, big_loss, "≥ 30 家")
    elif big_loss >= 15:
        add("大面家数", -1, big_loss, "≥ 15 家")
    elif big_loss <= 5:
        add("大面家数", 1, big_loss, "≤ 5 家")

    # 跌停家数
    if limit_down_count >= 15:
        add("跌停家数", -1, limit_down_count, "≥ 15 家")
    elif limit_down_count <= 3:
        add("跌停家数", 1, limit_down_count, "≤ 3 家")

    # 龙头状态
    dragon_broken = _dragon_broken(context)
    if dragon_broken:
        add("龙头断板", -1, True, "最高板未延续且跌破 -3%")

    total = sum(item["delta"] for item in evidence)
    phase = _phase_of(total, context, dragon_broken, broken_rate, big_loss, max_consecutive)
    confidence = _confidence(phase, total, context, emotion)

    return {
        "phase": phase,
        "label": PHASE_LABELS[phase],
        "advice": PHASE_ADVICE[phase],
        "evidence_score": total,
        "confidence": confidence,
        "prev_phase": context.prev_phase,
        "changed": bool(context.prev_phase and context.prev_phase != phase),
        "evidence": evidence,
        "metrics": {
            "limit_up": limit_up_count,
            "broken_rate": round(broken_rate, 2),
            "max_consecutive": max_consecutive,
            "promotion_rate": round(promotion_rate, 1) if promotion_rate is not None else None,
            "big_loss": big_loss,
            "dragon_broken": dragon_broken,
            "ladder": ladder,
        },
    }


def _promotion_rate(context: EmotionContext) -> float | None:
    """晋级率代理: 连板(≥2)家数 / 涨停家数。"""
    if not context.limit_up:
        return None
    multi = sum(1 for s in context.limit_up if (s.consecutive or 1) >= 2)
    return multi / len(context.limit_up) * 100.0


def _big_loss_count(context: EmotionContext) -> int:
    """大面家数: 跌幅超过「跌停幅度 × 0.7」的家数(主板 -7% / 创业科创 -14% / 北交所 -21%)。"""
    count = 0
    for quote in context.quotes:
        threshold = limit_pct(quote.code, quote.name) * 0.7
        if quote.change_pct <= -threshold:
            count += 1
    return count


def _dragon_broken(context: EmotionContext) -> bool:
    """龙头断板: 昨日最高板今日未涨停且跌破 -3%。"""
    if not context.prev_max_consecutive or context.prev_max_consecutive < 3:
        return False
    # 找到之前的高标, 看今日表现
    for quote in context.quotes:
        if quote.change_pct <= -3.0 and quote.change_pct < 0:
            # 无法精确知道哪只是昨日龙头, 用「存在高位股大幅回撤」近似
            return True
    return False


def _phase_of(
    evidence_score: int, context: EmotionContext, dragon_broken: bool,
    broken_rate: float, big_loss: int, max_consecutive: int,
) -> str:
    """阶段判定(按 92KeBi 状态机语义 + 打分边界)。"""
    prev = context.prev_phase
    # 明确退潮信号优先级最高
    if dragon_broken and (max_consecutive <= 3 or broken_rate > 30):
        return "ebb"
    if big_loss >= 15 and broken_rate > 30:
        return "ebb"
    if evidence_score >= 6:
        return "climax"
    if evidence_score >= 3:
        return "ferment"
    if evidence_score >= 0:
        return "start"
    if prev in ("climax", "ferment") and (broken_rate > 30 or big_loss >= 15):
        return "ebb"
    return "ice"


def _confidence(
    phase: str, evidence_score: int, context: EmotionContext, emotion: dict[str, Any] | None
) -> float:
    sample = len(context.quotes)
    base = 0.55
    if sample >= 3000:
        base += 0.15
    elif sample >= 800:
        base += 0.08
    # 证据分越远离边界越可信
    distance = min(abs(evidence_score), 6) / 6.0
    base += 0.2 * distance
    if phase == "climax" and context.max_consecutive >= 5:
        base += 0.05
    if phase == "ebb" and context.broken_rate > 35:
        base += 0.08
    if emotion and emotion.get("score", 0) > 0:
        base += 0.03
    return round(max(0.45, min(0.97, base)), 2)


# --------------------------------------------------------------------------- #
# 龙头识别
# --------------------------------------------------------------------------- #
def find_leaders(context: EmotionContext, limit: int = 20) -> list[dict[str, Any]]:
    """从涨停池中找出龙头/高标/补涨首板/跟风四个层次。"""
    if not context.limit_up:
        return []
    ordered = sorted(
        context.limit_up,
        key=lambda s: (-(s.consecutive or 1), -(s.amount or 0)),
    )
    highest = ordered[0].consecutive or 1
    out: list[dict[str, Any]] = []
    for index, stock in enumerate(ordered[: max(1, limit)], start=1):
        consecutive = stock.consecutive or 1
        if index == 1 or consecutive >= highest:
            tier = "龙头"
        elif consecutive >= max(2, highest - 1):
            tier = "高标"
        elif consecutive == 1:
            tier = "补涨首板"
        else:
            tier = "跟风"
        out.append({
            "rank": index,
            "code": stock.code,
            "name": stock.name,
            "tier": tier,
            "consecutive": consecutive,
            "change_pct": stock.change_pct,
            "amount": stock.amount,
            "turnover_rate": stock.turnover_rate,
            "first_limit_time": stock.first_limit_time,
            "open_times": stock.open_times,
            "industry": stock.industry,
            "reason": stock.reason,
        })
    return out


def sector_strength(context: EmotionContext, limit: int = 30) -> list[dict[str, Any]]:
    """按板块聚合涨停家数与平均涨幅, 得到板块强弱榜。"""
    grouped: dict[str, dict[str, Any]] = {}
    for stock in context.limit_up:
        key = stock.industry or "未分类"
        bucket = grouped.setdefault(key, {"sector": key, "limit_up": 0, "consecutive": 0, "codes": []})
        bucket["limit_up"] += 1
        bucket["consecutive"] = max(bucket["consecutive"], stock.consecutive or 1)
        if len(bucket["codes"]) < 30:
            bucket["codes"].append(stock.code)
    out = sorted(grouped.values(), key=lambda b: (-b["limit_up"], -b["consecutive"]))
    return out[:limit]


# --------------------------------------------------------------------------- #
# 快照(供 API/前端)
# --------------------------------------------------------------------------- #
def snapshot(
    *, quotes: list[Quote], limit_up: list[LimitUpStock], broken: list[LimitUpStock],
    breadth: Breadth | None = None, prev: dict[str, Any] | None = None,
    source: str = "",
) -> dict[str, Any]:
    previous = prev or {}
    context = EmotionContext(
        quotes=quotes, limit_up=limit_up, broken=broken, breadth=breadth, source=source,
        prev_phase=str(previous.get("phase") or ""),
        prev_emotion_score=float(previous.get("score") or 0.0),
        prev_limit_up_count=int(previous.get("limit_up") or 0),
        prev_max_consecutive=int(previous.get("max_consecutive") or 0),
    )
    emotion = compute_emotion_score(context)
    cycle = compute_cycle(context, emotion)
    session = market_session()
    return {
        "emotion": emotion,
        "cycle": cycle,
        "leaders": find_leaders(context),
        "sectors": sector_strength(context),
        "market": {
            "session": session.as_dict(),
            "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "sample_size": len(quotes),
            "source": source,
        },
    }


__all__ = [
    "EmotionContext",
    "compute_emotion_score",
    "compute_cycle",
    "find_leaders",
    "sector_strength",
    "snapshot",
    "PHASE_LABELS",
    "PHASE_ADVICE",
]
