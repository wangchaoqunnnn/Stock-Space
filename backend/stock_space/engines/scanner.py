"""扫描编排服务: 装配市场上下文 + 并发跑策略 + 落库。

这是连接"数据源"与"策略引擎"的中间层, 负责三件容易出错的事:

  1. **日线取数预算**: 全市场规模大, 每轮扫描只允许发起 ``scan_kline_budget`` 次日线
     网络请求(其余走磁盘缓存), 避免被上游封禁 IP;
  2. **上下文注入**: 把基准指数 20 日涨幅、人气排名、板块情绪、大盘环境闸门、
     财报数据一次性算好注入策略参数, 避免每只股票重复计算;
  3. **样本折算的透明化**: 当 ``universe_size`` 为有限值(小内存机器)时,
     市场级统计只覆盖样本, 响应里会带 ``sample_limited`` 与 ``universe_size`` 明确标注。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from ..config import config
from ..core.util import market_session, normalize_code, now_cn, today_str
from ..models import KLine, Quote
from ..providers.registry import AllProvidersFailed, registry
from ..store.kline_store import kline_store
from .base import Series, Signal, Strategy, all_strategies, build_series, get as get_strategy
from .indicators import safe_float, slope

logger = logging.getLogger(__name__)

#: 扫描进度回调：(已完成数, 总数)。见 scan() 的 on_progress 参数。
ProgressFn = Callable[[int, int], None]

#: 基准指数(相对强度与大盘闸门使用)
BENCHMARK_CODE = "000300"

#: 每只股票回看多少根日线
LOOKBACK_DAYS = 260


@dataclass
class MarketContext:
    """一次扫描所需的全部市场级上下文。"""

    trade_date: str = ""
    session: dict[str, Any] = field(default_factory=dict)
    quotes: list[Quote] = field(default_factory=list)
    quote_by_code: dict[str, Quote] = field(default_factory=dict)
    benchmark_ret20: float | None = None
    benchmark_ret5: float | None = None
    env_gate: str = "full"
    attention: dict[str, int] = field(default_factory=dict)
    sector_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    finance: dict[str, dict[str, Any]] = field(default_factory=dict)
    index_quotes: list[Quote] = field(default_factory=list)
    source: str = ""
    sample_limited: bool = False
    universe_size: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "session": self.session,
            "universe_size": self.universe_size,
            "sample_limited": self.sample_limited,
            "source": self.source,
            "benchmark_ret20": self.benchmark_ret20,
            "env_gate": self.env_gate,
            "sector_count": len(self.sector_stats),
            "attention_count": len(self.attention),
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# 市场上下文装配
# --------------------------------------------------------------------------- #
async def build_market_context(*, force: bool = False, with_extras: bool = True) -> MarketContext:
    """拉取全市场快照并算好所有上下文。"""
    context = MarketContext(trade_date=today_str())
    session = market_session()
    context.session = session.as_dict()

    try:
        quotes = await registry.snapshot(force=force)
    except AllProvidersFailed as exc:
        raise RuntimeError(f"无法获取全市场行情: {exc.detail}") from exc

    limit_n = config().universe_size
    if limit_n and len(quotes) > limit_n:
        # 按成交额降序截取, 保证活跃标的优先; 响应中会标注 sample_limited
        quotes = sorted(quotes, key=lambda q: -q.amount)[:limit_n]
        context.sample_limited = True
        context.warnings.append(
            f"配额 universe_size={limit_n} 限制了扫描范围, 市场级统计为样本口径"
        )
    context.quotes = quotes
    context.universe_size = len(quotes)
    context.quote_by_code = {q.code: q for q in quotes}
    context.source = quotes[0].source if quotes else ""

    # ---------------- 板块统计(用于共振与板块情绪) ----------------
    context.sector_stats = _sector_stats(quotes)

    # ---------------- 基准指数 + 大盘闸门 ----------------
    try:
        benchmark_kline = await registry.kline(BENCHMARK_CODE, 60, force=force)
        closes = np.asarray(benchmark_kline.closes, dtype=np.float64)
        if len(closes) >= 25:
            context.benchmark_ret20 = round((closes[-1] / closes[-21] - 1.0) * 100.0, 3)
            context.benchmark_ret5 = round((closes[-1] / closes[-6] - 1.0) * 100.0, 3)
            ma20 = float(np.mean(closes[-20:]))
            ma20_prev = float(np.mean(closes[-25:-5]))
            ma20_slope = ((ma20 - ma20_prev) / ma20_prev * 100.0) if ma20_prev else 0.0
            change20 = context.benchmark_ret20
            if change20 < -3.0:
                context.env_gate = "off"
            elif ma20_slope >= 0.05 and change20 >= -3.0:
                context.env_gate = "full"
            else:
                context.env_gate = "half"
    except Exception as exc:  # noqa: BLE001 - 基准取不到时按"中性"处理并告知用户
        logger.warning("基准指数 %s 获取失败: %s", BENCHMARK_CODE, exc)
        context.warnings.append(f"基准指数 {BENCHMARK_CODE} 不可用, 相对强度按中性处理")

    if not with_extras:
        return context

    # ---------------- 人气排名(可选能力) ----------------
    try:
        context.attention = await registry.attention()
    except Exception as exc:  # noqa: BLE001
        logger.debug("人气排名不可用: %s", exc)

    # ---------------- 财报(可选能力, 只对候选标的取) ----------------
    return context


def _sector_stats(quotes: Sequence[Quote]) -> dict[str, dict[str, float]]:
    """按行业聚合: 上涨占比 / 涨停占比 / 平均涨幅。"""
    grouped: dict[str, list[Quote]] = {}
    for quote in quotes:
        key = quote.industry or ""
        if key:
            grouped.setdefault(key, []).append(quote)
    stats: dict[str, dict[str, float]] = {}
    for name, members in grouped.items():
        if not members:
            continue
        up = sum(1 for q in members if q.change_pct > 0)
        limit_up = sum(1 for q in members if q.change_pct >= 9.5)
        avg = float(np.mean([q.change_pct for q in members]))
        stats[name] = {
            "count": float(len(members)),
            "up_ratio": up / len(members),
            "limit_up_ratio": limit_up / len(members),
            "avg_change_pct": avg,
        }
    return stats


# --------------------------------------------------------------------------- #
# 日线取数
# --------------------------------------------------------------------------- #
class KLineFetcher:
    """带预算与并发上限的日线批量取数。"""

    def __init__(self, *, budget: int | None = None, concurrency: int | None = None) -> None:
        self.budget = config().get("quotas.scan_kline_budget", 1200) if budget is None else budget
        self.concurrency = config().get("quotas.kline_concurrency", 16) if concurrency is None else concurrency
        self.used = 0
        self.disk_hits = 0
        self.network_hits = 0
        self.failures = 0
        self._lock = asyncio.Lock()

    async def fetch(self, code: str, *, days: int = LOOKBACK_DAYS,
                    force: bool = False) -> tuple[KLine | None, str]:
        """返回 ``(KLine|None, 来源)``; 来源为 ``disk`` / ``network`` / ``none``。"""
        code = normalize_code(code)
        if not force:
            cached = kline_store.get(code, days)
            if cached is not None and cached.bars:
                self.disk_hits += 1
                return cached, "disk"
        async with self._lock:
            if self.budget is not None and self.used >= self.budget:
                raise BudgetExhausted()
            self.used += 1
        try:
            kline = await registry.kline(code, days, force=True)
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            logger.debug("日线获取失败 %s: %s", code, exc)
            return None, "none"
        if kline is None or not kline.bars:
            self.failures += 1
            return None, "none"
        self.network_hits += 1
        try:
            kline_store.put(kline, source=kline.source)
        except Exception:  # noqa: BLE001 - 落盘失败不影响本次扫描
            pass
        return kline, "network"

    def report(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "used": self.used,
            "disk_hits": self.disk_hits,
            "network_hits": self.network_hits,
            "failures": self.failures,
        }


class BudgetExhausted(RuntimeError):
    """本轮日线请求配额已用完。"""


# --------------------------------------------------------------------------- #
# 单只评估
# --------------------------------------------------------------------------- #
def _inject_context(
    strategy: Strategy, params: Mapping[str, Any], context: MarketContext, quote: Quote,
) -> dict[str, Any]:
    """把市场级上下文注入策略参数(以 ``_`` 前缀的私有键传递)。"""
    merged = strategy.merged_params(params)
    merged["_benchmark_ret20"] = context.benchmark_ret20
    merged["_benchmark_ret5"] = context.benchmark_ret5
    merged["_env_gate"] = context.env_gate
    merged["_attention_rank"] = context.attention.get(quote.code)
    if quote.industry:
        stats = context.sector_stats.get(quote.industry)
        if stats:
            merged["_sector_context"] = stats
            merged["_sector_up_ratio"] = stats.get("up_ratio", 0.0)
    finance = context.finance.get(quote.code)
    if finance:
        merged["_finance"] = finance
    return merged


def evaluate_one(
    strategy: Strategy, quote: Quote, kline: KLine, params: Mapping[str, Any],
    context: MarketContext,
) -> Signal:
    """同步评估一只股票(CPU 密集, 请在 executor 里调用)。"""
    merged = _inject_context(strategy, params, context, quote)
    series = build_series(quote, kline)
    return strategy.evaluate(series, merged)


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
@dataclass
class ScanOutcome:
    strategy: str
    trade_date: str
    signals: list[Signal] = field(default_factory=list)
    total_evaluated: int = 0
    passed_count: int = 0
    duration_ms: float = 0.0
    fetcher: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self, *, limit: int = 100) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "trade_date": self.trade_date,
            "total_evaluated": self.total_evaluated,
            "passed_count": self.passed_count,
            "duration_ms": round(self.duration_ms, 1),
            "fetcher": self.fetcher,
            "context": self.context,
            "warnings": self.warnings,
            "items": [s.as_dict() for s in self.signals[:limit]],
        }


async def scan(
    strategy_key: str,
    *,
    params: Mapping[str, Any] | None = None,
    force: bool = False,
    limit: int = 100,
    context: MarketContext | None = None,
    codes: Sequence[str] | None = None,
    persist: bool = True,
    on_progress: ProgressFn | None = None,
) -> ScanOutcome:
    """对全市场(或指定代码)跑一个策略。

    ``on_progress(done, total)`` 会在**每只标的评估完成后**回调一次，
    供异步扫描任务把"跑到哪了"透出给前端进度条。

    为什么需要它：一次全市场扫描实测要 2~3 分钟，若进度只在"策略完成"时
    跳一次，进度条会长时间停在 0% —— 用户无法区分"在跑"和"卡死"。
    """
    strategy = get_strategy(strategy_key)
    if strategy is None:
        raise ValueError(f"未知策略: {strategy_key}")

    started = time.perf_counter()
    context = context or await build_market_context(force=force)

    targets: list[Quote] = list(context.quotes)
    if codes:
        wanted = {normalize_code(c) for c in codes}
        targets = [q for q in targets if q.code in wanted]
    if not targets:
        return ScanOutcome(strategy=strategy_key, trade_date=context.trade_date,
                           context=context.as_dict(), warnings=["没有可评估的标的"])

    fetcher = KLineFetcher()
    semaphore = asyncio.Semaphore(max(1, int(fetcher.concurrency)))
    #: 进度节流：每完成 1% 才回调一次，避免几千次回调把事件循环刷满
    total_targets = len(targets)
    done_count = 0
    progress_step = max(1, total_targets // 100)

    async def one(quote: Quote) -> Signal | None:
        nonlocal done_count
        async with semaphore:
            try:
                kline, _ = await fetcher.fetch(quote.code)
            except BudgetExhausted:
                return None
            if kline is None or len(kline.bars) < strategy.min_bars:
                return None
            try:
                return await asyncio.to_thread(
                    evaluate_one, strategy, quote, kline, params or {}, context
                )
            except Exception as exc:  # noqa: BLE001 - 单只异常不能中断整个扫描
                logger.debug("策略 %s 评估 %s 失败: %s", strategy_key, quote.code, exc)
                return None
            finally:
                #: finally 里计数，保证失败/跳过的标的也推进进度（否则会永远到不了 100%）
                done_count += 1
                if on_progress is not None and (done_count % progress_step == 0
                                                or done_count >= total_targets):
                    try:
                        on_progress(done_count, total_targets)
                    except Exception:  # noqa: BLE001 - 回调异常绝不影响扫描
                        logger.debug("扫描进度回调失败", exc_info=True)

    results = await asyncio.gather(*(one(q) for q in targets))
    signals = [s for s in results if s is not None]
    signals.sort(key=lambda s: (-s.score, s.code))

    outcome = ScanOutcome(
        strategy=strategy_key,
        trade_date=context.trade_date,
        signals=signals,
        total_evaluated=len(signals),
        passed_count=sum(1 for s in signals if s.passed),
        duration_ms=(time.perf_counter() - started) * 1000.0,
        fetcher=fetcher.report(),
        context=context.as_dict(),
    )
    if fetcher.budget is not None and fetcher.used >= fetcher.budget:
        outcome.warnings.append(
            f"本轮日线请求已达配额 {fetcher.budget}, 部分标的未参与评估; "
            "可在「设置」中调大 scan_kline_budget 或缩小 universe_size"
        )
    if persist:
        try:
            await asyncio.to_thread(persist_scan, outcome, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("扫描结果落库失败: %s", exc)
    return outcome


def persist_scan(outcome: ScanOutcome, *, limit: int = 100) -> int:
    """把扫描结果写入 ``scan_result`` 表(供历史对比与"最近一次扫描"接口)。"""
    from ..store.db import db

    rows = []
    now = time.time()
    for index, signal in enumerate(outcome.signals[:limit], start=1):
        rows.append((
            outcome.strategy, outcome.trade_date, signal.code, signal.name,
            signal.score, index, _json_dumps(signal.as_dict()),
            outcome.context.get("source", ""), now,
        ))
    if not rows:
        return 0
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM scan_result WHERE strategy=? AND trade_date=?",
            (outcome.strategy, outcome.trade_date),
        )
        conn.executemany(
            "INSERT INTO scan_result(strategy, trade_date, code, name, score, rank, "
            "payload, source, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def _json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


def last_scan(strategy_key: str, date: str | None = None) -> dict[str, Any] | None:
    """读取最近一次落库的扫描结果。"""
    from ..store.db import db

    if date:
        rows = db.query(
            "SELECT * FROM scan_result WHERE strategy=? AND trade_date=? ORDER BY rank",
            (strategy_key, date),
        )
    else:
        row = db.query_one(
            "SELECT trade_date FROM scan_result WHERE strategy=? ORDER BY trade_date DESC LIMIT 1",
            (strategy_key,),
        )
        if row is None:
            return None
        rows = db.query(
            "SELECT * FROM scan_result WHERE strategy=? AND trade_date=? ORDER BY rank",
            (strategy_key, row["trade_date"]),
        )
    if not rows:
        return None
    import json

    items = []
    for row in rows:
        try:
            items.append(json.loads(row["payload"]))
        except ValueError:
            continue
    return {
        "strategy": strategy_key,
        "trade_date": rows[0]["trade_date"],
        "source": rows[0]["source"],
        "created_at": rows[0]["created_at"],
        "items": items,
    }


# --------------------------------------------------------------------------- #
# 批量扫描(仪表盘用)
# --------------------------------------------------------------------------- #
async def scan_many(
    strategy_keys: Iterable[str],
    *,
    params_by_strategy: Mapping[str, Mapping[str, Any]] | None = None,
    force: bool = False,
    per_strategy_limit: int = 20,
    context: MarketContext | None = None,
) -> dict[str, ScanOutcome]:
    """顺序执行多个策略, 共享同一份市场上下文与日线缓存。"""
    shared = context or await build_market_context(force=force)
    out: dict[str, ScanOutcome] = {}
    for key in strategy_keys:
        params = (params_by_strategy or {}).get(key, {})
        try:
            out[key] = await scan(key, params=params, force=force,
                                  limit=per_strategy_limit, context=shared)
        except Exception as exc:  # noqa: BLE001 - 单个策略失败不影响其它策略
            logger.exception("策略 %s 扫描失败", key)
            out[key] = ScanOutcome(strategy=key, trade_date=shared.trade_date,
                                   context=shared.as_dict(), warnings=[f"扫描失败: {exc}"])
    return out


__all__ = [
    "MarketContext",
    "ScanOutcome",
    "KLineFetcher",
    "BudgetExhausted",
    "build_market_context",
    "evaluate_one",
    "scan",
    "scan_many",
    "last_scan",
    "persist_scan",
    "BENCHMARK_CODE",
    "LOOKBACK_DAYS",
]
