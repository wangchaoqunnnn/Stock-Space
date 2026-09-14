"""行情复盘报告生成 —— 源自 ReviewNotification + StockTradingReviewTool。

产出一份结构化复盘, 每个模块都有明确的 ``status``:

  * ``ok``        —— 数据齐全, 已填充;
  * ``degraded``  —— 部分字段缺失, 已注明缺什么;
  * ``missing``   —— 该模块所需数据源不可用。

**所有数字都来自数据层, 文案由规则生成** —— 这是刻意的: 复盘报告里最危险的是
"用语言编造数字"。本引擎不调用任何大模型, 因此不会出现"AI 幻觉出的涨幅"。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from ..core.util import MAJOR_INDICES, limit_pct, now_cn, today_str
from ..models import Breadth, LimitUpStock, Quote

logger = logging.getLogger(__name__)

#: 赚钱效应评级四档
RATINGS = ("极寒", "偏冷", "温和", "火热")

#: 禁止出现的荐股式表述(合规校验)。
#: ⚠️ 这些词必须**只针对股票/标的下达操作建议**时才算违规 ——
#: 平台自身会在策略说明、模块标题、风险提示里合法地使用"买入/卖出"等词
#: (例如"信号看板"里列出的买卖点、免责声明里的"不构成投资建议")。
#: 因此校验规则是:「禁止词」与「个股代码 / 六位数字」出现在同一句里才判定违规,
#: 否则会把平台自己的说明文案误判为荐股。
FORBIDDEN_PATTERNS = ("建议买入", "建议卖出", "建议加仓", "建议减仓", "目标价", "推荐买入", "推荐股票")

#: 出现这些片段说明是在做免责/风险提示, 整句豁免
_COMPLIANCE_EXEMPT = ("不构成", "风险", "免责", "仅为", "严禁", "不得", "策略规则", "买卖点", "历史")


def check_compliance(report: Mapping[str, Any]) -> dict[str, Any]:
    """合规校验。

    检查两件事:
      1. 是否对**具体个股**给出操作建议(禁止词 + 六位代码出现在同一句);
      2. 报告是否包含风险提示。
    """
    issues: list[str] = []
    for sentence in _sentences(report):
        if any(exempt in sentence for exempt in _COMPLIANCE_EXEMPT):
            continue
        if not re.search(r"(?<!\d)\d{6}(?!\d)", sentence):
            continue
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in sentence:
                issues.append(f"对具体标的出现疑似荐股表述「{pattern}」：{sentence[:60]}")
                break

    text = _flatten_text(report)
    if "不构成任何投资建议" not in text and "不构成投资建议" not in text:
        issues.append("缺少风险提示")
    return {"ok": not issues, "issues": issues}


def _sentences(node: Any) -> list[str]:
    """把报告拆成句子, 用于"同一句内同时出现禁止词与代码"的判断。"""
    text = _flatten_text(node)
    parts: list[str] = []
    for chunk in re.split(r"[。；;\n]", text):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


@dataclass
class Module:
    key: str
    title: str
    status: str = "ok"           # ok / degraded / missing
    note: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    bullets: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "status": self.status,
            "note": self.note, "data": self.data, "bullets": self.bullets,
        }


# --------------------------------------------------------------------------- #
# 评级
# --------------------------------------------------------------------------- #
def money_effect_rating(breadth: Breadth, limit_up: int, broken: int, max_consecutive: int) -> tuple[str, str]:
    """赚钱效应四档评级(带评分与理由)。"""
    score = 0
    if limit_up >= 100:
        score += 2
    elif limit_up >= 60:
        score += 1
    total_attempts = limit_up + broken
    broken_rate = (broken / total_attempts * 100.0) if total_attempts else 0.0
    if broken_rate <= 15:
        score += 1
    elif broken_rate >= 40:
        score -= 2
    elif broken_rate > 25:
        score -= 1
    ratio = (breadth.up / breadth.down) if breadth.down else float(breadth.up)
    if ratio >= 2:
        score += 1
    elif ratio <= 0.6:
        score -= 1
    if max_consecutive >= 7:
        score += 1
    elif max_consecutive <= 2:
        score -= 1
    if breadth.limit_down >= 15:
        score -= 1

    if score >= 4:
        rating = "火热"
    elif score >= 1:
        rating = "温和"
    elif score >= -2:
        rating = "偏冷"
    else:
        rating = "极寒"
    reason = (
        f"涨停 {limit_up} 家、炸板率 {broken_rate:.1f}%、涨跌比 "
        f"{ratio:.2f}、最高连板 {max_consecutive} 板、跌停 {breadth.limit_down} 家 "
        f"→ 综合得分 {score}"
    )
    return rating, reason


def trend_stage(index_closes: Sequence[float]) -> tuple[str, str]:
    """行情阶段判定(基于指数均线与量价代理)。"""
    closes = np.asarray(list(index_closes), dtype=np.float64)
    closes = closes[np.isfinite(closes)]
    if len(closes) < 65:
        return "数据不足", "指数历史不足 65 个交易日, 无法判定阶段"
    close = float(closes[-1])
    ma20 = float(np.mean(closes[-20:]))
    ma60 = float(np.mean(closes[-60:]))
    ma20_prev = float(np.mean(closes[-25:-5]))
    ma20_rising = ma20 > ma20_prev
    pos = 0
    if close >= ma20:
        pos += 1
    else:
        pos -= 1
    pos += 1 if ma20_rising else -1
    if close >= ma60:
        pos += 1
    if pos >= 3:
        stage = "反弹途中"
    elif pos == 2:
        stage = "震荡修复(偏多)"
    elif pos >= 0:
        stage = "震荡整理"
    elif pos == -1:
        stage = "震荡筑底"
    else:
        stage = "弱势调整(情绪退潮)"
    detail = (
        f"收盘 {close:.2f}｜MA20 {ma20:.2f}({'上行' if ma20_rising else '下行'})"
        f"｜MA60 {ma60:.2f}｜阶段得分 {pos}"
    )
    return stage, detail


# --------------------------------------------------------------------------- #
# 报告生成
# --------------------------------------------------------------------------- #
@dataclass
class ReviewInput:
    quotes: list[Quote] = field(default_factory=list)
    breadth: Breadth | None = None
    limit_up: list[LimitUpStock] = field(default_factory=list)
    broken: list[LimitUpStock] = field(default_factory=list)
    index_quotes: list[Quote] = field(default_factory=list)
    index_kline: list[float] = field(default_factory=list)
    sector_flow: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    emotion: dict[str, Any] = field(default_factory=dict)
    cycle: dict[str, Any] = field(default_factory=dict)
    overseas: dict[str, Any] = field(default_factory=dict)
    source: str = ""


def build_report(payload: ReviewInput, *, kind: str = "cn_close") -> dict[str, Any]:
    """生成结构化复盘。``kind``: ``cn_close``(A股收盘复盘) / ``us_open``(隔夜外围前瞻)。"""
    if kind == "us_open":
        return _build_overseas(payload)

    modules: list[Module] = []
    quotes = payload.quotes
    breadth = payload.breadth or _breadth_from_quotes(quotes)
    limit_up = payload.limit_up
    broken = payload.broken
    max_consecutive = max((s.consecutive or 1) for s in limit_up) if limit_up else 0

    # ---------------- 模块 1: 大盘指数概览 ----------------
    index_module = Module(key="indices", title="大盘指数概览")
    index_data: list[dict[str, Any]] = []
    for name, code, market in MAJOR_INDICES[:5]:
        match = next((q for q in payload.index_quotes if q.code == code), None)
        if match is None:
            match = next((q for q in quotes if q.code == code), None)
        if match is not None and match.price > 0:
            index_data.append({
                "name": name, "code": code, "price": match.price,
                "change_pct": match.change_pct, "change": match.change,
                "amount": match.amount,
            })
    if index_data:
        index_module.data = {
            "indices": index_data,
            "total_amount": round(breadth.total_amount, 0),
        }
        index_module.bullets = [
            f"{item['name']} {item['price']:.2f}（{item['change_pct']:+.2f}%）"
            for item in index_data
        ]
    else:
        index_module.status = "missing"
        index_module.note = "指数行情不可用(快照源未返回指数代码)"
    modules.append(index_module)

    # ---------------- 模块 2: 情绪温度计 ----------------
    emotion_module = Module(key="sentiment", title="市场情绪温度计")
    rating, rating_reason = money_effect_rating(breadth, len(limit_up), len(broken), max_consecutive)
    ladder: dict[str, int] = {}
    for stock in limit_up:
        level = str(max(1, stock.consecutive or 1))
        ladder[level] = ladder.get(level, 0) + 1
    attempts = len(limit_up) + len(broken)
    emotion_module.data = {
        "rating": rating,
        "rating_reason": rating_reason,
        "up": breadth.up, "down": breadth.down, "flat": breadth.flat,
        "up_ratio": breadth.up_ratio,
        "limit_up": len(limit_up) or breadth.limit_up,
        "limit_down": breadth.limit_down,
        "broken": len(broken),
        "broken_rate": round(len(broken) / attempts * 100.0, 2) if attempts else 0.0,
        "max_consecutive": max_consecutive,
        "ladder": ladder,
        "emotion_score": (payload.emotion or {}).get("score"),
        "emotion_level": (payload.emotion or {}).get("level"),
        "cycle_phase": (payload.cycle or {}).get("label"),
        "cycle_confidence": (payload.cycle or {}).get("confidence"),
    }
    emotion_module.bullets = [
        f"赚钱效应：{rating}（{rating_reason}）",
        f"涨跌分布：上涨 {breadth.up} / 平盘 {breadth.flat} / 下跌 {breadth.down}"
        f"（上涨占比 {breadth.up_ratio * 100:.1f}%）",
        f"涨停 {emotion_module.data['limit_up']} 家、跌停 {breadth.limit_down} 家、"
        f"炸板 {len(broken)} 家（炸板率 {emotion_module.data['broken_rate']}%）",
        f"连板梯队：最高 {max_consecutive} 板 {ladder}",
    ]
    if payload.emotion:
        emotion_module.bullets.append(
            f"加权情绪分 {payload.emotion.get('score')}（{payload.emotion.get('level')}），"
            f"周期阶段 {emotion_module.data['cycle_phase']}"
        )
    if not quotes and not limit_up:
        emotion_module.status = "missing"
        emotion_module.note = "缺少行情与涨停池数据"
    elif not limit_up:
        emotion_module.status = "degraded"
        emotion_module.note = "涨停池不可用, 涨停家数由快照推算"
    modules.append(emotion_module)

    # ---------------- 模块 3: 板块轮动 ----------------
    sector_module = Module(key="sectors", title="板块轮动图谱")
    gainers = sorted([q for q in quotes if q.change_pct > 0], key=lambda q: -q.change_pct)[:200]
    by_industry: dict[str, list[Quote]] = {}
    for quote in quotes:
        if quote.industry:
            by_industry.setdefault(quote.industry, []).append(quote)
    sector_rows = []
    for name, members in by_industry.items():
        if len(members) < 3:
            continue
        change = float(np.mean([m.change_pct for m in members]))
        leader = max(members, key=lambda m: m.change_pct)
        sector_rows.append({
            "name": name, "change_pct": round(change, 3),
            "amount": sum(m.amount for m in members),
            "leader_name": leader.name, "leader_code": leader.code,
            "leader_change_pct": round(leader.change_pct, 3),
            "up_count": sum(1 for m in members if m.change_pct > 0),
            "count": len(members),
        })
    sector_rows.sort(key=lambda row: -row["change_pct"])
    if sector_rows:
        sector_module.data = {
            "top": sector_rows[:10],
            "bottom": sector_rows[-10:][::-1],
            "flow": payload.sector_flow[:20],
        }
        sector_module.bullets = [
            "涨幅前列：" + "、".join(
                f"{row['name']} {row['change_pct']:+.2f}%（龙头 {row['leader_name']}）"
                for row in sector_rows[:5]
            ),
            "跌幅前列：" + "、".join(
                f"{row['name']} {row['change_pct']:+.2f}%"
                for row in sector_rows[-5:][::-1]
            ),
            "板块数据由当前快照的行业字段聚合(样本口径)，不代表完整行业指数。",
        ]
    else:
        sector_module.status = "degraded"
        sector_module.note = "快照未返回行业字段, 板块轮动无法聚合"
    modules.append(sector_module)

    # ---------------- 模块 4: 行情阶段 ----------------
    stage_module = Module(key="stage", title="行情阶段客观解读")
    stage, stage_detail = trend_stage(payload.index_kline)
    stage_module.data = {"stage": stage, "detail": stage_detail}
    stage_module.bullets = [f"阶段判定：{stage}", stage_detail]
    if stage == "数据不足":
        stage_module.status = "degraded"
        stage_module.note = "指数历史数据不足"
    modules.append(stage_module)

    # ---------------- 模块 5: 资讯与公告 ----------------
    news_module = Module(key="news", title="重要资讯与公告")
    if payload.news:
        news_module.data = {"items": payload.news[:15]}
        news_module.bullets = [
            f"[{item.get('channel', '')}] {item.get('title', '')}"
            for item in payload.news[:8]
        ]
    else:
        news_module.status = "missing"
        news_module.note = "资讯/公告源当前不可用或尚未抓取"
    modules.append(news_module)

    # ---------------- 总结 ----------------
    conclusion = _conclusion(modules, rating, stage, breadth, len(limit_up))
    report = {
        "kind": kind,
        "title": f"A股收盘复盘（{today_str()}）",
        "trade_date": today_str(),
        "generated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "source": payload.source,
        "rating": rating,
        "stage": stage,
        "modules": [m.as_dict() for m in modules],
        "conclusion": conclusion,
        "risk_notice": "本文仅为行情复盘参考，不构成任何投资建议。股市有风险，投资需谨慎。",
    }
    report["compliance"] = check_compliance(report)
    return report


def _build_overseas(payload: ReviewInput) -> dict[str, Any]:
    """隔夜外围前瞻(数据不可得时如实标注, 不编造)。"""
    modules = [
        Module(
            key="overseas", title="隔夜外围资产联动",
            status="missing" if not payload.overseas else "ok",
            note="" if payload.overseas else "外围行情源未接入或当前不可用(请在「数据源」页面启用)",
            data=payload.overseas,
            bullets=[
                f"{key}：{value}" for key, value in list((payload.overseas or {}).items())[:10]
            ],
        ),
        Module(
            key="news", title="隔夜重要资讯",
            status="ok" if payload.news else "missing",
            data={"items": payload.news[:15]},
            bullets=[f"{item.get('title', '')}" for item in payload.news[:8]],
        ),
    ]
    return {
        "kind": "us_open",
        "title": f"隔夜外围市场前瞻（{today_str()}）",
        "trade_date": today_str(),
        "generated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "source": payload.source,
        "modules": [m.as_dict() for m in modules],
        "conclusion": "外围数据源为可选能力，未接入时本报告仅列出可用资讯。",
        "risk_notice": "本文仅为隔夜外围行情参考，不构成任何投资建议。股市有风险，投资需谨慎。",
        "compliance": {"ok": True, "issues": []},
    }


def _breadth_from_quotes(quotes: Sequence[Quote]) -> Breadth:
    up = sum(1 for q in quotes if q.change_pct > 0)
    down = sum(1 for q in quotes if q.change_pct < 0)
    flat = len(quotes) - up - down
    limit_up = limit_down = 0
    for quote in quotes:
        threshold = limit_pct(quote.code, quote.name)
        if quote.change_pct >= threshold - 0.3:
            limit_up += 1
        elif quote.change_pct <= -(threshold - 0.3):
            limit_down += 1
    return Breadth(up=up, down=down, flat=max(0, flat), limit_up=limit_up,
                   limit_down=limit_down, total_amount=sum(q.amount for q in quotes),
                   source="snapshot_derived")


def _conclusion(
    modules: Iterable[Module], rating: str, stage: str, breadth: Breadth, limit_up: int
) -> str:
    missing = [m.title for m in modules if m.status == "missing"]
    degraded = [m.title for m in modules if m.status == "degraded"]
    parts = [
        f"赚钱效应 {rating}，行情阶段 {stage}。",
        f"全市场上涨 {breadth.up} 家、下跌 {breadth.down} 家，涨停 {limit_up} 家。",
    ]
    if missing:
        parts.append(f"以下模块因数据源不可用而缺失：{'、'.join(missing)}。")
    if degraded:
        parts.append(f"以下模块数据不完整：{'、'.join(degraded)}。")
    parts.append("所有结论均为对历史数据的统计描述，不构成投资建议。")
    return "".join(parts)


def check_compliance(report: Mapping[str, Any]) -> dict[str, Any]:
    """合规校验。

    检查两件事:
      1. 是否对**具体个股**给出操作建议(禁止词 + 六位代码出现在同一句);
      2. 报告是否包含风险提示。
    """
    issues: list[str] = []
    for sentence in _sentences(report):
        if any(exempt in sentence for exempt in _COMPLIANCE_EXEMPT):
            continue
        if not re.search(r"(?<!\d)\d{6}(?!\d)", sentence):
            continue
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in sentence:
                issues.append(f"对具体标的出现疑似荐股表述「{pattern}」：{sentence[:60]}")
                break

    text = _flatten_text(report)
    if "不构成任何投资建议" not in text and "不构成投资建议" not in text:
        issues.append("缺少风险提示")
    return {"ok": not issues, "issues": issues}


def _sentences(node: Any) -> list[str]:
    """把报告拆成句子, 用于"同一句内同时出现禁止词与代码"的判断。"""
    text = _flatten_text(node)
    parts: list[str] = []
    for chunk in re.split(r"[。；;\n]", text):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def _flatten_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, Mapping):
        return " ".join(_flatten_text(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return " ".join(_flatten_text(v) for v in node)
    return ""


# --------------------------------------------------------------------------- #
# 推送用摘要
# --------------------------------------------------------------------------- #
def summary_text(report: Mapping[str, Any], *, max_len: int = 180) -> str:
    """给企业微信推送用的一句话摘要(优先用 conclusion)。"""
    text = str(report.get("conclusion") or "").strip()
    if not text:
        parts = []
        for module in report.get("modules") or []:
            if module.get("status") == "ok":
                bullets = module.get("bullets") or []
                if bullets:
                    parts.append(str(bullets[0]))
                if len(parts) >= 3:
                    break
        text = "｜".join(parts)
    rating = report.get("rating")
    prefix = f"赚钱效应：{rating}｜" if rating else ""
    merged = f"{prefix}{text}"
    return merged[:max_len]


__all__ = [
    "ReviewInput", "Module", "build_report", "summary_text",
    "money_effect_rating", "trend_stage", "check_compliance", "RATINGS",
]
