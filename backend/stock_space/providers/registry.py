"""数据源注册中心: 能力路由 + 健康度排序 + 自动切换 + 手动锁定。

对应需求 8「获取数据需要有多个源…支持用户手动刷新数据、手动切换源」。

调度策略(每个能力独立):
  1. 候选 = 配置的顺序 ∩ 已注册 ∩ 支持该能力 ∩ 可用(已安装/凭据齐备);
  2. 若用户对某能力**手动锁定**了某个源且该源健康, 则强制用它;
  3. 其余按 `健康度分`(成功率 50% + 近期滑窗 50%, 再与延迟得分加权) 降序,
     并让被熔断的源沉底 —— 这样"变慢/变坏"的源会自然退出主路径;
  4. 依次尝试, 首个**通过校验**的结果即返回; 每次真实切换都会记录
     `switch_events`, 前端「数据源」页面可看到切换历史与原因;
  5. 全部失败时抛 ``AllProvidersFailed``(携带每个源的具体错误), 绝不静默返回空。

**校验是必须的**: 实测"不报错但数据不全"比直接报错更常见
(例如腾讯日线对北交所只返回 1 根、新浪列表缺北交所), 所以每个能力都带
``validator``, 不通过视同失败并继续切换。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..config import config
from ..core.http import HttpClient, ProviderError, http_client, metrics_board
from ..core.memory import memory_guard
from ..core.cache import BoundedTTLCache
from ..models import KLine, Quote
from .akshare import AkshareProvider
from .ashare import AshareProvider
from .base import (
    ALL_CAPABILITIES,
    CAP_ANNOUNCEMENT,
    CAP_ATTENTION,
    CAP_BREADTH,
    CAP_CODE_LIST,
    CAP_FINANCE,
    CAP_INDICES,
    CAP_KLINE,
    CAP_LIMIT_UP_POOL,
    CAP_MINUTE,
    CAP_MONEY_FLOW,
    CAP_NEWS_FLASH,
    CAP_QUOTE,
    CAP_RANK,
    CAP_SECTOR,
    CAP_SECTOR_FLOW,
    CAP_SECTOR_MEMBERS,
    CAP_SNAPSHOT,
    Provider,
)
from .eastmoney import EastmoneyProvider
from .sina import SinaProvider
from .social import CninfoProvider, WeiboProvider, XueqiuProvider
from .synthetic import SyntheticProvider
from .tencent import TencentProvider
from .ths import ThsProvider

logger = logging.getLogger(__name__)


class AllProvidersFailed(RuntimeError):
    """某能力下所有数据源均失败。"""

    def __init__(self, capability: str, errors: list[tuple[str, str]]) -> None:
        self.capability = capability
        self.errors = errors
        detail = "; ".join(f"{name}: {msg}" for name, msg in errors) or "无可用数据源"
        super().__init__(f"[{capability}] 全部数据源不可用 -> {detail}")
        self.detail = detail


#: 能力 -> (方法名, kwargs 构造器)
_CALLS: dict[str, tuple[str, Callable[[tuple, dict], dict]]] = {
    CAP_SNAPSHOT: ("fetch_snapshot", lambda a, k: {}),
    CAP_QUOTE: ("fetch_quotes", lambda a, k: {"codes": a[0] if a else k.get("codes", [])}),
    CAP_KLINE: ("fetch_kline", lambda a, k: {
        "code": a[0] if a else k.get("code", ""),
        "days": (a[1] if len(a) > 1 else k.get("days", 260)),
        "period": k.get("period", "day"),
    }),
    CAP_MINUTE: ("fetch_minute", lambda a, k: {"code": a[0] if a else k.get("code", "")}),
    CAP_RANK: ("fetch_rank", lambda a, k: {
        "kind": a[0] if a else k.get("kind", "gainers"),
        "limit": (a[1] if len(a) > 1 else k.get("limit", 50)),
    }),
    CAP_SECTOR: ("fetch_sectors", lambda a, k: {"kind": a[0] if a else k.get("kind", "industry")}),
    CAP_SECTOR_MEMBERS: ("fetch_sector_members", lambda a, k: {
        "sector_code": a[0] if a else k.get("sector_code", ""),
        "sector_name": (a[1] if len(a) > 1 else k.get("sector_name", "")),
    }),
    CAP_MONEY_FLOW: ("fetch_money_flow", lambda a, k: {"code": a[0] if a else k.get("code", "")}),
    CAP_SECTOR_FLOW: ("fetch_sector_flow", lambda a, k: {"limit": a[0] if a else k.get("limit", 30)}),
    CAP_LIMIT_UP_POOL: ("fetch_limit_up_pool", lambda a, k: {}),
    CAP_BREADTH: ("fetch_breadth", lambda a, k: {}),
    CAP_CODE_LIST: ("fetch_code_list", lambda a, k: {}),
    CAP_ATTENTION: ("fetch_attention", lambda a, k: {}),
    CAP_NEWS_FLASH: ("fetch_news_flash", lambda a, k: {"limit": a[0] if a else k.get("limit", 50)}),
    CAP_ANNOUNCEMENT: ("fetch_announcements", lambda a, k: {"limit": a[0] if a else k.get("limit", 50)}),
    CAP_FINANCE: ("fetch_finance", lambda a, k: {"codes": a[0] if a else k.get("codes", [])}),
    CAP_INDICES: ("fetch_indices", lambda a, k: {"specs": a[0] if a else k.get("specs", [])}),
}

#: 能力 -> 结果校验器(不通过视同失败并切换源)
_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    CAP_SNAPSHOT: lambda r: isinstance(r, list) and len(r) >= 100,
    CAP_CODE_LIST: lambda r: isinstance(r, list) and len(r) >= 100,
    CAP_QUOTE: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_KLINE: lambda r: r is not None and len(getattr(r, "bars", [])) >= 20,
    CAP_MINUTE: lambda r: isinstance(r, dict) and len(r.get("points") or []) > 0,
    CAP_RANK: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_SECTOR: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_SECTOR_MEMBERS: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_MONEY_FLOW: lambda r: r is not None,
    CAP_SECTOR_FLOW: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_LIMIT_UP_POOL: lambda r: isinstance(r, dict) and bool(r.get("limit_up") or r.get("broken")),
    CAP_BREADTH: lambda r: r is not None and (r.up + r.down + r.flat) > 0,
    CAP_ATTENTION: lambda r: isinstance(r, dict) and len(r) > 0,
    CAP_NEWS_FLASH: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_ANNOUNCEMENT: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_FINANCE: lambda r: isinstance(r, list) and len(r) > 0,
    CAP_INDICES: lambda r: isinstance(r, list) and len(r) > 0
    and any(getattr(q, "price", 0) > 0 for q in r),
}


@dataclass
class CallOutcome:
    """一次能力调用的完整结果 —— 让调用方知道"到底用了哪个源、试过谁"。"""

    capability: str
    value: Any
    alias: str
    attempts: list[dict[str, Any]]

    @property
    def source(self) -> str:
        return self.alias


class ProviderRegistry:
    """数据源注册与调度中心。"""

    #: 全市场快照缓存键(只缓存一份)
    SNAPSHOT_KEY = "__all_market__"

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._lock = threading.RLock()
        self._locked: dict[str, str] = {}
        self._disabled: set[str] = set()
        self._last_used: dict[str, str] = {}
        self._built = False
        #: 全市场快照的"在途"锁 —— 避免多个并发请求各拉一遍全市场（约 8 秒）。
        #: 实测：仪表盘会同时触发快照与涨跌家数两路请求，若不去重就会拉两遍。
        self._snapshot_lock = asyncio.Lock()

        self.snapshot_cache: BoundedTTLCache[list[Quote]] = BoundedTTLCache(
            name="provider_snapshot", max_entries=2, ttl_seconds=60.0
        )
        self.quote_cache: BoundedTTLCache[Quote] = BoundedTTLCache(
            name="provider_quote", max_entries=6000, ttl_seconds=30.0
        )
        self.kline_cache: BoundedTTLCache[KLine] = BoundedTTLCache(
            name="provider_kline", max_entries=1500, ttl_seconds=900.0
        )
        self.misc_cache: BoundedTTLCache[Any] = BoundedTTLCache(
            name="provider_misc", max_entries=400, ttl_seconds=300.0
        )
        memory_guard.register_all(
            [self.snapshot_cache, self.quote_cache, self.kline_cache, self.misc_cache]
        )

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def build(self, client: HttpClient | None = None, *, force: bool = False) -> ProviderRegistry:
        with self._lock:
            if self._built and not force:
                return self
            http = client or http_client
            self._providers.clear()
            for cls in (
                EastmoneyProvider, SinaProvider, TencentProvider, ThsProvider,
                AkshareProvider, AshareProvider, CninfoProvider, XueqiuProvider,
                WeiboProvider, SyntheticProvider,
            ):
                try:
                    provider = cls(http)  # type: ignore[operator]
                except Exception as exc:  # noqa: BLE001 - 单个源构造失败不影响其它源
                    logger.warning("数据源 %s 初始化失败: %s", cls.__name__, exc)
                    continue
                self._providers[provider.name] = provider
            self._built = True
            logger.info(
                "已注册数据源: %s",
                ", ".join(f"{p.name}({len(p.capabilities)})" for p in self._providers.values()),
            )
            self._apply_locks()
            return self

    def _apply_locks(self) -> None:
        self._locked = dict(config().locked_sources())

    def reload(self) -> ProviderRegistry:
        """配置变更(网页保存设置)后重新读取锁定项与顺序。"""
        self._apply_locks()
        return self

    def register(self, provider: Provider) -> None:
        with self._lock:
            self._providers[provider.name] = provider
            self._built = True

    def get(self, name: str) -> Provider | None:
        return self._providers.get(name)

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    # ------------------------------------------------------------------ #
    # 候选排序
    # ------------------------------------------------------------------ #
    def _is_enabled(self, provider: Provider) -> bool:
        if provider.name in self._disabled:
            return False
        if provider.name == "synthetic":
            return config().synthetic_allowed  # 只有显式开启才允许合成数据
        meta = _provider_meta(provider.name)
        if meta.get("enabled") is False:
            return False
        return True

    def candidates(self, capability: str) -> list[str]:
        """按健康度排序后的候选源名列表。"""
        with self._lock:
            # 演示模式: 只使用合成数据源, 绝不混入真实源
            # (否则会看到"真实源失败 → 落到合成数据"的混合结果, 极易误判)
            if config().synthetic_allowed:
                synthetic = self._providers.get("synthetic")
                if synthetic is not None and synthetic.supports(capability):
                    return ["synthetic"]
                return []

            configured = config().capability_order(capability)
            if not configured:
                configured = [
                    name for name, provider in self._providers.items()
                    if provider.supports(capability)
                ]
            usable: list[str] = []
            for name in configured:
                provider = self._providers.get(name)
                if provider is None or not provider.supports(capability):
                    continue
                if not self._is_enabled(provider) or not provider.usable:
                    continue
                usable.append(name)
            if not usable:
                return []

            # 用户锁定优先(但仍要在候选内)
            locked = self._locked.get(capability)
            if locked and locked in usable:
                rest = self._sort_by_health([n for n in usable if n != locked])
                return [locked] + rest
            return self._sort_by_health(usable)

    def _sort_by_health(self, names: list[str]) -> list[str]:
        """按健康度排序, 但**尊重用户配置的顺序作为主序**。

        为什么主序必须由配置决定: 配置里已经把"字段更全/口径更合适"的源排在前面
        (例如腾讯行情带量比与涨跌停价、新浪不带), 如果让健康度完全重排,
        一个稍慢但字段完整的源会被一个快但字段残缺的源挤掉, 结果是"看起来更快、
        但策略拿不到需要的字段"。健康度只在**同分位**内做微调, 让变坏的源自然下沉。
        """
        def key(name: str) -> tuple[float, int, int]:
            provider = self._providers.get(name)
            metric = metrics_board.get(name)
            priority = getattr(provider, "priority", 50)
            return (metric.health_score(), 0 if metric.is_cooling() else 1, priority)

        # 配置顺序为主序, 仅用健康度做稳定微调: 把健康度明显更差(<60)的源后移
        ordered = list(names)
        configured_rank = {name: index for index, name in enumerate(ordered)}
        health = {name: metrics_board.get(name).health_score() for name in ordered}
        cooling = {name: metrics_board.get(name).is_cooling() for name in ordered}
        return sorted(
            ordered,
            key=lambda name: (
                0 if (health[name] >= 60.0 and not cooling[name]) else 1,
                configured_rank[name],
            ),
        )

    def _legacy_health_sort(self, names: list[str]) -> list[str]:
        """纯粹的按健康度排序(保留给"重新探测/复位熔断"后的激进模式)。"""
        def key(name: str) -> tuple[float, int, int]:
            provider = self._providers.get(name)
            metric = metrics_board.get(name)
            priority = getattr(provider, "priority", 50)
            return (metric.health_score(), 0 if metric.is_cooling() else 1, priority)

        return sorted(names, key=key, reverse=True)

    # ------------------------------------------------------------------ #
    # 核心调用
    # ------------------------------------------------------------------ #
    async def call(
        self,
        capability: str,
        *args: Any,
        force: bool = False,
        **kwargs: Any,
    ) -> CallOutcome:
        if not self._built:
            self.build()
        spec = _CALLS.get(capability)
        if spec is None:
            raise ValueError(f"未知能力: {capability}")
        method_name, arg_builder = spec
        validator = _VALIDATORS.get(capability)

        candidates = self.candidates(capability)
        if not candidates:
            # 把"为什么没有候选"讲清楚, 而不是给一个空列表
            reasons = []
            for name, provider in self._providers.items():
                if not provider.supports(capability):
                    continue
                if not provider.installed():
                    reasons.append(f"{name}: 未安装")
                elif provider.login_required_for_use and not provider.has_credentials():
                    reasons.append(f"{name}: 未配置登录凭据")
                elif not self._is_enabled(provider):
                    reasons.append(f"{name}: 已被禁用")
            raise AllProvidersFailed(capability, reasons or [("(none)", "没有源声明支持该能力")])

        errors: list[tuple[str, str]] = []
        attempts: list[dict[str, Any]] = []
        primary = candidates[0]

        for index, alias in enumerate(candidates):
            provider = self._providers[alias]
            method = getattr(provider, method_name, None)
            if method is None:
                errors.append((alias, f"未实现 {method_name}"))
                attempts.append({"source": alias, "ok": False, "error": "方法未实现"})
                continue

            metric = metrics_board.get(alias)
            if metric.is_cooling() and not force:
                remaining = metric.cooling_remaining()
                errors.append((alias, f"熔断冷却中(剩余 {remaining:.0f}s)"))
                attempts.append({"source": alias, "ok": False,
                                 "error": f"熔断冷却中(剩余 {remaining:.0f}s)"})
                continue

            provider.use_metric(alias)
            started = time.perf_counter()
            try:
                call_kwargs = arg_builder(args, kwargs)
                value = await method(**call_kwargs)
                if validator is not None and not validator(value):
                    raise ProviderError("结果未通过校验(数据不完整或条数异常)", source=alias)
            except Exception as exc:  # noqa: BLE001 - 任何异常都应触发切换
                latency = (time.perf_counter() - started) * 1000.0
                message = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ProviderError) else str(exc)
                errors.append((alias, message))
                attempts.append({"source": alias, "ok": False,
                                 "error": message[:200], "latency_ms": round(latency, 1)})
                # 指标由 HttpClient 记录; 这里只补记"网络成功但校验不通过"的情况
                if validator is not None and isinstance(exc, ProviderError) and "校验" in str(exc):
                    metric.record(False, latency, error=message)
                continue
            finally:
                provider.clear_metric()

            latency = (time.perf_counter() - started) * 1000.0
            self._last_used[capability] = alias
            attempts.append({"source": alias, "ok": True, "latency_ms": round(latency, 1)})
            if index > 0:
                reason = errors[-1][1] if errors else "主源不可用"
                metrics_board.record_switch(capability, primary, alias, reason)
                logger.warning("数据源切换 [%s]: %s -> %s (原因: %s)",
                               capability, primary, alias, reason[:120])
            return CallOutcome(capability=capability, value=value, alias=alias, attempts=attempts)

        # 全部失败: 只打印一条汇总日志, 避免逐只刷屏
        logger.warning("能力 [%s] 全部数据源失败: %s", capability, _brief(errors))
        raise AllProvidersFailed(capability, errors)

    # ------------------------------------------------------------------ #
    # 便捷封装(带缓存)
    # ------------------------------------------------------------------ #
    async def snapshot(self, *, force: bool = False) -> list[Quote]:
        """全市场快照（进程内缓存 + 在途去重）。

        两个都必要：
          * **缓存**：命中直接返回，避免每 30 秒都重拉 60 页；
          * **在途锁**：首个请求执行期间，后续并发请求等待并复用同一结果 ——
            实测仪表盘会同时发起"快照"和"涨跌家数"两路请求，没有这个锁就会
            并行拉两遍全市场（各约 8 秒），既慢又容易触发上游限流。
        """
        if not force:
            cached = self.snapshot_cache.get(self.SNAPSHOT_KEY)
            if cached is not None:
                return cached

        async with self._snapshot_lock:
            # 拿到锁后先复查：可能已经被前一个等待者填好了
            if not force:
                cached = self.snapshot_cache.get(self.SNAPSHOT_KEY)
                if cached is not None:
                    return cached
            outcome = await self.call(CAP_SNAPSHOT, force=force)
            value = outcome.value
            ttl = 30.0 if _in_trading_session() else config().get("refresh.idle_ttl_seconds", 300)
            self.snapshot_cache.ttl_seconds = float(ttl)
            self.snapshot_cache.set(self.SNAPSHOT_KEY, value)
            return value

    def cached_snapshot(self) -> list[Quote] | None:
        """只读快照缓存，不发起网络请求（供"手里已经有快照"的调用方复用）。"""
        return self.snapshot_cache.get(self.SNAPSHOT_KEY)

    async def quotes(self, codes: Iterable[str], *, force: bool = False) -> list[Quote]:
        targets = [str(c) for c in codes]
        if not targets:
            return []
        out: list[Quote] = []
        missing: list[str] = []
        if not force:
            for code in targets:
                cached = self.quote_cache.get(code)
                if cached is not None:
                    out.append(cached)
                else:
                    missing.append(code)
        else:
            missing = targets

        if missing:
            outcome = await self.call(CAP_QUOTE, missing, force=force)
            fetched = outcome.value
            ttl = 30.0 if _in_trading_session() else 300.0
            self.quote_cache.ttl_seconds = ttl
            for quote in fetched:
                self.quote_cache.set(quote.code, quote)
            out.extend(fetched)
        return out

    async def kline(self, code: str, days: int = 260, *, force: bool = False,
                    period: str = "day") -> KLine:
        key = f"{code}:{days}:{period}"
        if not force:
            cached = self.kline_cache.get(key)
            if cached is not None:
                return cached
        if period == "day":
            outcome = await self.call(CAP_KLINE, code, days, period=period, force=force)
            value = outcome.value
        else:
            # 周线/月线统一由日线本地聚合得到 —— 只依赖各源都稳定支持的日线接口,
            # 避免"某个源不支持 weekly"导致整条链路换源。
            result = await self.call(CAP_KLINE, code, days, period=period, force=force)
            value = result.value
            value = self._resample(value, period, code, days)
        if value is not None:
            self.kline_cache.set(key, value)
        return value

    @staticmethod
    def _resample(value: KLine | None, period: str, code: str, days: int) -> KLine | None:
        """把日线聚合成周线/月线; 若上游已返回目标周期则原样使用。"""
        if value is None:
            return None
        if getattr(value, "period", "day") == period:
            return value
        from ..store.kline_store import aggregate_bars

        bars = aggregate_bars(value.bars, period)
        if not bars:
            return value
        return KLine(code=code, name=value.name, period=period, bars=bars[-days:],
                     source=f"{value.source}+{period}", fetched_at=value.fetched_at)

    async def rank(self, kind: str = "gainers", limit: int = 50) -> list[Quote]:
        outcome = await self.call(CAP_RANK, kind, limit)
        return outcome.value

    async def sectors(self, kind: str = "industry") -> list[Any]:
        key = f"sectors:{kind}"
        cached = self.misc_cache.get(key)
        if cached is not None:
            return cached
        outcome = await self.call(CAP_SECTOR, kind)
        self.misc_cache.set(key, outcome.value)
        return outcome.value

    async def sector_members(self, sector_code: str, sector_name: str = "") -> list[Quote]:
        try:
            outcome = await self.call(CAP_SECTOR_MEMBERS, sector_code, sector_name)
            return outcome.value
        except AllProvidersFailed as exc:
            # 兜底: 用已缓存的全市场快照按行业名派生(零额外网络请求)
            derived = self._derive_members(sector_name)
            if derived:
                metrics_board.record_switch(
                    CAP_SECTOR_MEMBERS, "(专用接口)", "(快照派生)", str(exc)[:120]
                )
                return derived
            raise

    def _derive_members(self, sector_name: str) -> list[Quote]:
        if not sector_name:
            return []
        snapshot = self.snapshot_cache.peek(self.SNAPSHOT_KEY) or []
        return [q for q in snapshot if q.industry and q.industry == sector_name]

    async def breadth(self) -> Any:
        """市场涨跌家数（走能力路由）。

        注意：调用方**优先复用自己已有的快照**（见 ``services.market_service.breadth``），
        不要默认走这里 —— 东财的涨跌家数需要重新分页拉全市场（约 7 秒），
        而快照通常已经在手上了。
        """
        outcome = await self.call(CAP_BREADTH)
        return outcome.value

    async def limit_up_pool(self) -> dict[str, Any]:
        key = "limit_up_pool"
        cached = self.misc_cache.get(key)
        if cached is not None:
            return cached
        outcome = await self.call(CAP_LIMIT_UP_POOL)
        ttl = 60.0 if _in_trading_session() else 600.0
        self.misc_cache.set(key, outcome.value, ttl=ttl)
        return outcome.value

    async def news(self, limit: int = 60) -> list[Any]:
        outcome = await self.call(CAP_NEWS_FLASH, limit)
        return outcome.value

    async def announcements(self, limit: int = 40) -> list[Any]:
        outcome = await self.call(CAP_ANNOUNCEMENT, limit)
        return outcome.value

    async def attention(self) -> dict[str, int]:
        """关注度是可选能力: 全失败时返回空字典, 不抛错。"""
        try:
            outcome = await self.call(CAP_ATTENTION)
            return outcome.value if isinstance(outcome.value, dict) else {}
        except AllProvidersFailed as exc:
            logger.info("关注度不可用(使用代理指标兜底): %s", exc)
            return {}

    # ------------------------------------------------------------------ #
    # 手动控制
    # ------------------------------------------------------------------ #
    def lock(self, capability: str, alias: str | None) -> dict[str, Any]:
        """手动锁定/解锁某能力的数据源。``alias=None`` 表示恢复自动择优。"""
        if capability not in ALL_CAPABILITIES:
            raise ValueError(f"未知能力: {capability}")
        with self._lock:
            previous = self._locked.get(capability)
            if alias is None:
                self._locked.pop(capability, None)
            else:
                provider = self._providers.get(alias)
                if provider is None:
                    raise ValueError(f"未知数据源: {alias}")
                if not provider.supports(capability):
                    raise ValueError(f"数据源 {alias} 不支持能力 {capability}")
                if not provider.installed():
                    raise ValueError(f"数据源 {alias} 未安装, 无法锁定")
                self._locked[capability] = alias
            current = self._locked.get(capability)
        return {"capability": capability, "previous": previous, "locked": current}

    def unlock_all(self) -> None:
        with self._lock:
            self._locked.clear()

    def locks(self) -> dict[str, str]:
        return dict(self._locked)

    def disable(self, alias: str, disabled: bool = True) -> None:
        with self._lock:
            if disabled:
                self._disabled.add(alias)
            else:
                self._disabled.discard(alias)

    def disabled(self) -> list[str]:
        return sorted(self._disabled)

    def reset_breakers(self) -> int:
        return metrics_board.reset()

    def clear_caches(self) -> int:
        return (
            self.snapshot_cache.clear() + self.quote_cache.clear()
            + self.kline_cache.clear() + self.misc_cache.clear()
        )

    # ------------------------------------------------------------------ #
    # 报告
    # ------------------------------------------------------------------ #
    def provider_health(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for provider in sorted(self._providers.values(), key=lambda p: -p.priority):
            health = provider.health().as_dict()
            health["usable"] = provider.usable
            health["enabled"] = self._is_enabled(provider)
            health["disabled_manually"] = provider.name in self._disabled
            health["metrics"] = metrics_board.get(provider.name).as_dict()
            out.append(health)
        return out

    def report(self) -> dict[str, Any]:
        categories: dict[str, Any] = {}
        for capability in ALL_CAPABILITIES:
            candidates = self.candidates(capability)
            entries: list[dict[str, Any]] = []
            for rank, alias in enumerate(candidates, start=1):
                provider = self._providers.get(alias)
                metric = metrics_board.get(alias).as_dict()
                metric.update({
                    "rank": rank, "alias": alias,
                    "label": getattr(provider, "label", alias),
                    "active": rank == 1,
                })
                entries.append(metric)
            categories[capability] = {
                "locked": self._locked.get(capability),
                "last_used": self._last_used.get(capability),
                "configured_order": config().capability_order(capability),
                "effective_order": candidates,
                "providers": entries,
                "has_fallback": len(candidates) > 1,
            }
        return {
            "mode": config().source_mode,
            "categories": categories,
            "providers": self.provider_health(),
            "recent_switches": metrics_board.recent_switches(20),
            "blocked_hosts": metrics_board.blocked_hosts(),
            "cache": {
                "snapshot": self.snapshot_cache.stats().as_dict(),
                "quote": self.quote_cache.stats().as_dict(),
                "kline": self.kline_cache.stats().as_dict(),
                "misc": self.misc_cache.stats().as_dict(),
            },
            "locks": dict(self._locked),
            "disabled": sorted(self._disabled),
        }

    async def probe_all(self, capability: str = CAP_QUOTE) -> list[dict[str, Any]]:
        """逐个源的连通性自检(「数据源」页面的"全部探测"按钮)。"""
        results: list[dict[str, Any]] = []
        for provider in self._providers.values():
            if not provider.installed():
                results.append({"name": provider.name, "label": provider.label,
                                "ok": False, "message": "未安装", "latency_ms": 0.0})
                continue
            provider.use_metric(provider.name)
            try:
                result = await provider.probe()
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "latency_ms": 0.0, "message": f"{type(exc).__name__}: {exc}"}
            finally:
                provider.clear_metric()
            results.append({"name": provider.name, "label": provider.label, **result})
        return results


def _provider_meta(name: str) -> dict[str, Any]:
    from .endpoints import endpoints

    return endpoints.meta(name)


def _brief(errors: list[tuple[str, str]], limit: int = 3) -> str:
    """把失败原因压缩成一行, 避免日志被同一条错误刷屏。"""
    items = [f"{name}: {msg[:70]}" for name, msg in errors[:limit]]
    if len(errors) > limit:
        items.append(f"...(共 {len(errors)} 个源失败)")
    return "; ".join(items)


def _in_trading_session() -> bool:
    from ..core.util import market_session

    return market_session().is_trading


#: 全局单例
registry = ProviderRegistry()


__all__ = ["ProviderRegistry", "registry", "AllProvidersFailed", "CallOutcome"]
