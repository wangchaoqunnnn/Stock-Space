"""数据源接口定义。

每个数据源(Provider)声明自己**支持哪些能力(capability)**, 注册中心按能力调度。
这样"某个源缺少某能力"不会导致整体不可用 —— 例如腾讯的排行榜不含科创板,
但它的批量实时行情很稳, 于是它只参与 ``quote``/``kline``, 不参与 ``rank``。

能力清单与对应方法名约定:

    snapshot -> fetch_snapshot()            全市场快照
    quote    -> fetch_quotes(codes)         批量实时行情
    kline    -> fetch_kline(code, days)     日/周/月 K 线
    minute   -> fetch_minute(code)          当日分时
    rank     -> fetch_rank(kind, limit)     榜单(gainers/losers/amount/turnover/speed)
    sector   -> fetch_sectors(kind)         板块列表
    sector_members -> fetch_sector_members(sector_code, sector_name)
    money_flow -> fetch_money_flow(code)    个股资金流
    sector_flow -> fetch_sector_flow(limit) 板块资金流排名
    limit_up_pool -> fetch_limit_up_pool()  涨停/炸板池
    breadth  -> fetch_breadth()             市场涨跌家数
    code_list -> fetch_code_list()          全市场代码表
    attention -> fetch_attention()          人气/关注度
    news_flash -> fetch_news_flash()        快讯
    announcement -> fetch_announcements()   公告
    finance  -> fetch_finance(codes)        财务指标
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from ..config import config
from ..core.http import HttpClient, http_client
from ..models import ProviderHealth

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 能力常量
# --------------------------------------------------------------------------- #
CAP_SNAPSHOT = "snapshot"
CAP_QUOTE = "quote"
CAP_KLINE = "kline"
CAP_MINUTE = "minute"
CAP_RANK = "rank"
CAP_SECTOR = "sector"
CAP_SECTOR_MEMBERS = "sector_members"
CAP_MONEY_FLOW = "money_flow"
CAP_SECTOR_FLOW = "sector_flow"
CAP_LIMIT_UP_POOL = "limit_up_pool"
CAP_BREADTH = "breadth"
CAP_CODE_LIST = "code_list"
CAP_ATTENTION = "attention"
CAP_NEWS_FLASH = "news_flash"
CAP_ANNOUNCEMENT = "announcement"
CAP_FINANCE = "finance"
CAP_INDICES = "indices"
CAP_HEALTH = "health"

#: 能力 -> (方法名, 中文说明)
CAPABILITY_SPECS: dict[str, tuple[str, str]] = {
    CAP_SNAPSHOT: ("fetch_snapshot", "全市场快照"),
    CAP_QUOTE: ("fetch_quotes", "批量实时行情"),
    CAP_INDICES: ("fetch_indices", "指数行情"),
    CAP_KLINE: ("fetch_kline", "历史K线"),
    CAP_MINUTE: ("fetch_minute", "当日分时"),
    CAP_RANK: ("fetch_rank", "涨跌/成交/换手榜单"),
    CAP_SECTOR: ("fetch_sectors", "板块列表"),
    CAP_SECTOR_MEMBERS: ("fetch_sector_members", "板块成分股"),
    CAP_MONEY_FLOW: ("fetch_money_flow", "个股资金流"),
    CAP_SECTOR_FLOW: ("fetch_sector_flow", "板块资金流"),
    CAP_LIMIT_UP_POOL: ("fetch_limit_up_pool", "涨停/炸板池"),
    CAP_BREADTH: ("fetch_breadth", "市场涨跌家数"),
    CAP_CODE_LIST: ("fetch_code_list", "全市场代码表"),
    CAP_ATTENTION: ("fetch_attention", "人气/关注度"),
    CAP_NEWS_FLASH: ("fetch_news_flash", "财经快讯"),
    CAP_ANNOUNCEMENT: ("fetch_announcements", "公司公告"),
    CAP_FINANCE: ("fetch_finance", "财务指标"),
    CAP_HEALTH: ("probe", "连通性探测"),
}

ALL_CAPABILITIES: tuple[str, ...] = tuple(CAPABILITY_SPECS.keys())


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #
class Provider:
    """数据源基类。子类只需实现自己支持的方法, 并在 ``capabilities`` 中声明。"""

    #: 唯一标识(与 config/sources.toml 的 [providers.<name>] 对应)
    name: str = "base"
    #: 中文名(前端展示)
    label: str = "基础数据源"
    #: 能力集合
    capabilities: frozenset[str] = frozenset()
    #: 基础权重(数值越大越优先), 会被健康度动态调整
    priority: int = 50
    #: 是否需要登录态凭据
    requires_login: bool = False
    #: 一个源在无凭据时是否仍然可用(例如雪球缺 Cookie 就不可用)
    login_required_for_use: bool = False
    #: 说明文案
    note: str = ""
    homepage: str = ""

    def __init__(self, client: HttpClient | None = None) -> None:
        self.http = client or http_client
        self._metrics_alias: str | None = None

    # ------------------------------ 通用 ------------------------------
    def supports(self, capability: str) -> bool:
        # 所有数据源都支持连通性探测(probe), 这样 /api/datasources 才能逐源自检
        if capability == CAP_HEALTH:
            return True
        return capability in self.capabilities

    @property
    def capabilities_ordered(self) -> list[str]:
        # health 是隐含能力, 不列在展示用的能力清单里(避免误导用户以为它是一条取数通道)
        return [cap for cap in ALL_CAPABILITIES if cap != CAP_HEALTH and cap in self.capabilities]

    def installed(self) -> bool:
        """库型数据源(akshare/ashare)在未安装时返回 False。"""
        return True

    def credential(self, key: str, default: str = "") -> str:
        """读取用户在「数据源」页面手工填入的凭据。"""
        creds = config().credentials().get(self.name)
        if isinstance(creds, dict):
            value = creds.get(key)
            if value:
                return str(value)
        return default

    def has_credentials(self) -> bool:
        if not self.requires_login:
            return True
        creds = config().credentials().get(self.name)
        if not isinstance(creds, dict) or not creds:
            return False
        return any(str(v).strip() for v in creds.values())

    @property
    def usable(self) -> bool:
        if not self.installed():
            return False
        if self.login_required_for_use and not self.has_credentials():
            return False
        return True

    # ------------------------------ 指标别名 ------------------------------
    def use_metric(self, alias: str) -> None:
        """让 http 客户端把指标记在 ``alias``(能力入口)上, 而非 Provider 名。"""
        self._metrics_alias = alias

    def clear_metric(self) -> None:
        self._metrics_alias = None

    @property
    def alias(self) -> str:
        return self._metrics_alias or self.name

    # ------------------------------ 健康 ------------------------------
    def health(self, *, capability: str = CAP_HEALTH) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            label=self.label,
            installed=self.installed(),
            enabled=True,
            capabilities=self.capabilities_ordered,
            priority=self.priority,
            requires_login=self.requires_login,
            logged_in=self.has_credentials(),
            note=self.note,
            homepage=self.homepage,
        )

    async def probe(self) -> dict[str, Any]:
        """连通性自检 —— 默认实现: 尝试拉一次小批量行情。"""
        from ..core.util import normalize_code

        started = time.perf_counter()
        try:
            quotes = await self.fetch_quotes(["600519"])
            latency = (time.perf_counter() - started) * 1000.0
            ok = bool(quotes) and quotes[0].price > 0
            return {
                "ok": ok,
                "latency_ms": round(latency, 1),
                "message": "连通正常" if ok else "返回数据为空",
                "sample": quotes[0].as_dict() if quotes else None,
            }
        except Exception as exc:  # noqa: BLE001
            latency = (time.perf_counter() - started) * 1000.0
            return {
                "ok": False,
                "latency_ms": round(latency, 1),
                "message": f"{type(exc).__name__}: {exc}",
            }

    # ------------------------------ 未实现的方法 ------------------------------
    async def fetch_snapshot(self) -> list[Any]:
        raise NotImplementedError

    async def fetch_quotes(self, codes: Iterable[str]) -> list[Any]:
        raise NotImplementedError

    async def fetch_indices(self, specs: Iterable[tuple[str, str]]) -> list[Any]:
        """指数行情。``specs`` 为 ``(显示名, 代码)`` 列表。

        **必须与个股行情分开**: 指数与个股代码段重叠(000001 既是上证指数也是平安银行),
        用股票接口的代码前缀规则去推导会拿到错误的标的。
        """
        raise NotImplementedError

    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> Any:
        raise NotImplementedError

    async def fetch_minute(self, code: str) -> Any:
        raise NotImplementedError

    async def fetch_rank(self, kind: str = "gainers", limit: int = 50) -> list[Any]:
        raise NotImplementedError

    async def fetch_sectors(self, kind: str = "industry") -> list[Any]:
        raise NotImplementedError

    async def fetch_sector_members(self, sector_code: str, sector_name: str = "") -> list[Any]:
        raise NotImplementedError

    async def fetch_money_flow(self, code: str) -> Any:
        raise NotImplementedError

    async def fetch_sector_flow(self, limit: int = 30) -> list[Any]:
        raise NotImplementedError

    async def fetch_limit_up_pool(self) -> dict[str, Any]:
        raise NotImplementedError

    async def fetch_breadth(self) -> Any:
        raise NotImplementedError

    async def fetch_code_list(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def fetch_attention(self) -> dict[str, int]:
        raise NotImplementedError

    async def fetch_news_flash(self, limit: int = 50) -> list[Any]:
        raise NotImplementedError

    async def fetch_announcements(self, limit: int = 50) -> list[Any]:
        raise NotImplementedError

    async def fetch_finance(self, codes: Iterable[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r} caps={len(self.capabilities)}>"


class Unsupported(Exception):
    """数据源不支持请求的能力。"""

    def __init__(self, provider: str, capability: str) -> None:
        super().__init__(f"数据源 {provider} 不支持能力 {capability}")
        self.provider = provider
        self.capability = capability


__all__ = [
    "Provider",
    "Unsupported",
    "CAPABILITY_SPECS",
    "ALL_CAPABILITIES",
    "CAP_SNAPSHOT",
    "CAP_QUOTE",
    "CAP_INDICES",
    "CAP_KLINE",
    "CAP_MINUTE",
    "CAP_RANK",
    "CAP_SECTOR",
    "CAP_SECTOR_MEMBERS",
    "CAP_MONEY_FLOW",
    "CAP_SECTOR_FLOW",
    "CAP_LIMIT_UP_POOL",
    "CAP_BREADTH",
    "CAP_CODE_LIST",
    "CAP_ATTENTION",
    "CAP_NEWS_FLASH",
    "CAP_ANNOUNCEMENT",
    "CAP_FINANCE",
    "CAP_HEALTH",
]
