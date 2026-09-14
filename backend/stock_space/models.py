"""平台领域模型。

全部使用 ``dataclass`` + ``as_dict()`` 的形式: 既能让策略引擎获得属性访问的便利,
又能零成本序列化成前端 JSON。字段一律使用英文命名, 中文只出现在 ``label`` 类字段里。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
@dataclass
class Quote:
    """实时/延时行情快照。"""

    code: str
    name: str = ""
    market: str = ""
    board: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0          # 手
    amount: float = 0.0          # 元
    turnover_rate: float = 0.0   # 换手率 %
    volume_ratio: float = 0.0    # 量比
    amplitude: float = 0.0       # 振幅 %
    pe_ttm: float = 0.0
    pb: float = 0.0
    total_mv: float = 0.0        # 总市值(元)
    float_mv: float = 0.0        # 流通市值(元)
    speed: float = 0.0           # 涨速 %(5 分钟)
    limit_up: float = 0.0
    limit_down: float = 0.0
    is_st: bool = False
    industry: str = ""
    source: str = ""
    ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["price"] = round(self.price, 3)
        data["change"] = round(self.change, 3)
        data["change_pct"] = round(self.change_pct, 3)
        return data


@dataclass
class Bar:
    """单根 K 线(前复权)。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0
    change_pct: float = 0.0
    turnover_rate: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "open": round(self.open, 3),
            "high": round(self.high, 3),
            "low": round(self.low, 3),
            "close": round(self.close, 3),
            "volume": round(self.volume, 2),
            "amount": round(self.amount, 2),
            "change_pct": round(self.change_pct, 3),
            "turnover_rate": round(self.turnover_rate, 3),
        }


@dataclass
class KLine:
    code: str
    name: str = ""
    period: str = "day"
    bars: list[Bar] = field(default_factory=list)
    source: str = ""
    fetched_at: float = 0.0

    @property
    def closes(self) -> list[float]:
        return [b.close for b in self.bars]

    @property
    def highs(self) -> list[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> list[float]:
        return [b.low for b in self.bars]

    @property
    def opens(self) -> list[float]:
        return [b.open for b in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [b.volume for b in self.bars]

    def as_dict(self, limit: int | None = None) -> dict[str, Any]:
        bars = self.bars[-limit:] if limit else self.bars
        return {
            "code": self.code,
            "name": self.name,
            "period": self.period,
            "source": self.source,
            "count": len(self.bars),
            "bars": [b.as_dict() for b in bars],
        }


@dataclass
class IndexQuote:
    code: str
    name: str
    price: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    amount: float = 0.0
    market: str = "cn"
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "price": round(self.price, 3),
            "change": round(self.change, 3),
            "change_pct": round(self.change_pct, 3),
            "amount": round(self.amount, 2),
            "market": self.market,
            "source": self.source,
        }


# --------------------------------------------------------------------------- #
# 板块 / 资金
# --------------------------------------------------------------------------- #
@dataclass
class SectorQuote:
    code: str
    name: str
    change_pct: float = 0.0
    amount: float = 0.0
    turnover_rate: float = 0.0
    up_count: int = 0
    down_count: int = 0
    leader_name: str = ""
    leader_change_pct: float = 0.0
    main_net_inflow: float = 0.0
    kind: str = "industry"
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["change_pct"] = round(self.change_pct, 3)
        return data


@dataclass
class MoneyFlow:
    code: str
    name: str = ""
    main_net: float = 0.0
    main_net_pct: float = 0.0
    super_net: float = 0.0
    large_net: float = 0.0
    medium_net: float = 0.0
    small_net: float = 0.0
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 市场宽度 / 情绪
# --------------------------------------------------------------------------- #
@dataclass
class Breadth:
    up: int = 0
    down: int = 0
    flat: int = 0
    limit_up: int = 0
    limit_down: int = 0
    broken_board: int = 0      # 炸板家数
    up_over_5: int = 0
    down_over_5: int = 0
    total_amount: float = 0.0
    source: str = ""

    @property
    def total(self) -> int:
        return self.up + self.down + self.flat

    @property
    def up_ratio(self) -> float:
        return round(self.up / self.total, 4) if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "total": self.total,
            "up_ratio": self.up_ratio,
        }


@dataclass
class LimitUpStock:
    code: str
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    amount: float = 0.0
    turnover_rate: float = 0.0
    first_limit_time: str = ""
    last_limit_time: str = ""
    open_times: int = 0          # 开板次数
    consecutive: int = 1         # 连板数
    industry: str = ""
    reason: str = ""
    is_broken: bool = False
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NewsItem:
    id: str
    title: str
    summary: str = ""
    url: str = ""
    source: str = ""
    channel: str = "news"        # news / announcement / rumor / flash
    published_at: str = ""
    related_codes: list[str] = field(default_factory=list)
    important: bool = False
    sentiment: str = "neutral"
    pushed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderHealth:
    name: str
    label: str
    installed: bool = True
    enabled: bool = True
    capabilities: list[str] = field(default_factory=list)
    priority: int = 50
    requires_login: bool = False
    logged_in: bool = False
    note: str = ""
    homepage: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "Quote",
    "Bar",
    "KLine",
    "IndexQuote",
    "SectorQuote",
    "MoneyFlow",
    "Breadth",
    "LimitUpStock",
    "NewsItem",
    "ProviderHealth",
]
