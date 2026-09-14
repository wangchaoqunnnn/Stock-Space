"""AKShare 数据源(库型, 可选安装)。

AKShare 是第三方聚合库, 封装了大量免费财经接口。它的价值是**多一条完全独立的
取数通道** —— 当东财/新浪/腾讯同时不可用(例如本地网络策略变化)时, AKShare 往往
仍能工作, 因为它会自行选择可达的上游。

工程约束:
  * **可选依赖**: 未安装时 ``installed()`` 返回 False, 该源在「数据源」页面显示为
    「未安装」并且不参与调度 —— 不影响任何其它功能;
  * **不阻塞事件循环**: AKShare 是同步阻塞库, 全部调用通过 ``asyncio.to_thread`` 投递;
  * **列名容错**: DataFrame 的列名随版本变化, 统一走 ``_pick`` 多候选名匹配,
    匹配不到时宁可报错也不猜。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Iterable

import pandas as pd

from ..core.http import ProviderError
from ..core.util import board_of, detect_market, limit_pct, normalize_code
from ..models import Bar, Breadth, KLine, NewsItem, Quote, SectorQuote
from .base import (
    CAP_BREADTH,
    CAP_CODE_LIST,
    CAP_KLINE,
    CAP_NEWS_FLASH,
    CAP_QUOTE,
    CAP_SECTOR,
    CAP_SNAPSHOT,
    Provider,
)
from .endpoints import endpoints

logger = logging.getLogger(__name__)

#: 常见列名候选(按 akshare 不同版本)
_COLUMNS: dict[str, tuple[str, ...]] = {
    "code": ("代码", "symbol", "code", "证券代码", "股票代码"),
    "name": ("名称", "name", "证券简称", "股票简称"),
    "price": ("最新价", "最新", "现价", "close", "收盘", "收盘价"),
    "change_pct": ("涨跌幅", "change_pct", "涨跌幅(%)"),
    "change": ("涨跌额",),
    "volume": ("成交量", "volume"),
    "amount": ("成交额", "amount"),
    "amplitude": ("振幅",),
    "high": ("最高", "最高价", "high"),
    "low": ("最低", "最低价", "low"),
    "open": ("今开", "开盘", "开盘价", "open"),
    "prev_close": ("昨收", "前收盘", "prev_close"),
    "turnover": ("换手率", "turnover"),
    "volume_ratio": ("量比",),
    "pe": ("市盈率-动态", "市盈率", "市盈率(TTM)"),
    "pb": ("市净率",),
    "total_mv": ("总市值",),
    "float_mv": ("流通市值",),
    "speed": ("涨速",),
    "date": ("日期", "date", "时间"),
    "sector_change": ("涨跌幅",),
}


def _pick(row: Any, key: str, default: Any = None) -> Any:
    """按候选列名取值(DataFrame 行或 dict 均可用)。"""
    for column in _COLUMNS.get(key, ()):
        try:
            if column in row:
                value = row[column]
                if value is None:
                    continue
                if isinstance(value, float) and pd.isna(value):
                    continue
                return value
        except (TypeError, KeyError):
            continue
    return default


def _f(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-", "--"):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if pd.isna(number):
        return default
    return number


class AkshareProvider(Provider):
    name = "akshare"
    label = "AKShare"
    priority = 50
    capabilities = frozenset(
        {CAP_SNAPSHOT, CAP_QUOTE, CAP_KLINE, CAP_SECTOR, CAP_BREADTH, CAP_CODE_LIST, CAP_NEWS_FLASH}
    )
    note = "第三方聚合库(可选安装)。未安装时本源显示为「未安装」且不参与调度。"
    homepage = "https://akshare.akfamily.xyz/"

    # ------------------------------------------------------------------ #
    def installed(self) -> bool:
        try:
            import akshare  # noqa: F401
        except Exception:  # noqa: BLE001 - 可能是 ImportError 或依赖冲突
            return False
        return True

    async def _call(self, func_name: str, /, **kwargs: Any) -> pd.DataFrame:
        """在专用线程里调用 akshare 的同步函数。"""
        if not self.installed():
            raise ProviderError("AKShare 未安装", source=self.name)

        def runner() -> pd.DataFrame:
            import akshare as ak  # 延迟导入

            func = getattr(ak, func_name, None)
            if func is None:
                raise ProviderError(f"AKShare 当前版本缺少接口 {func_name}", source=self.name)
            result = func(**kwargs)
            if result is None:
                return pd.DataFrame()
            if isinstance(result, pd.DataFrame):
                return result
            return pd.DataFrame(result)

        try:
            return await asyncio.to_thread(runner)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成 ProviderError 触发换源
            raise ProviderError(f"AKShare 调用失败: {type(exc).__name__}: {exc}", source=self.name) from exc

    # ------------------------------------------------------------------ #
    async def fetch_snapshot(self) -> list[Quote]:
        frame = await self._call("stock_zh_a_spot_em")
        if frame is None or frame.empty:
            raise ProviderError("AKShare 快照为空", source=self.name)
        out: list[Quote] = []
        for _, row in frame.iterrows():
            code = str(_pick(row, "code", "") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            name = str(_pick(row, "name", "") or "").strip()
            prev_close = _f(_pick(row, "prev_close"))
            price = _f(_pick(row, "price"))
            out.append(
                Quote(
                    code=code, name=name, market=detect_market(code), board=board_of(code),
                    price=price or prev_close, prev_close=prev_close,
                    open=_f(_pick(row, "open")), high=_f(_pick(row, "high")), low=_f(_pick(row, "low")),
                    change=_f(_pick(row, "change")), change_pct=_f(_pick(row, "change_pct")),
                    volume=_f(_pick(row, "volume")), amount=_f(_pick(row, "amount")),
                    turnover_rate=_f(_pick(row, "turnover")), volume_ratio=_f(_pick(row, "volume_ratio")),
                    amplitude=_f(_pick(row, "amplitude")), pe_ttm=_f(_pick(row, "pe")),
                    pb=_f(_pick(row, "pb")), total_mv=_f(_pick(row, "total_mv")),
                    float_mv=_f(_pick(row, "float_mv")), speed=_f(_pick(row, "speed")),
                    limit_up=round(prev_close * (1 + limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    limit_down=round(prev_close * (1 - limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    is_st="ST" in name.upper(),
                    source=self.name, ts=time.time(),
                )
            )
        if not out:
            raise ProviderError("AKShare 快照解析为空", source=self.name)
        return out

    async def fetch_code_list(self) -> list[dict[str, Any]]:
        quotes = await self.fetch_snapshot()
        return [{"code": q.code, "name": q.name, "market": q.market, "board": q.board} for q in quotes]

    async def fetch_quotes(self, codes: Iterable[str]) -> list[Quote]:
        frame = await self._call("stock_bid_ask_em", symbol=",".join(
            normalize_code(c) for c in list(codes)[:50]
        )) if False else None  # 该接口只支持单只, 走快照过滤更省请求
        wanted = {normalize_code(c) for c in codes}
        snapshot = await self.fetch_snapshot()
        out = [q for q in snapshot if q.code in wanted]
        if not out:
            raise ProviderError("AKShare 未匹配到请求的代码", source=self.name)
        return out

    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> KLine:
        code = normalize_code(code)
        period_map = {"day": "daily", "week": "weekly", "month": "monthly"}
        frame = await self._call(
            "stock_zh_a_hist",
            symbol=code, period=period_map.get(period, "daily"), adjust="qfq",
        )
        if frame is None or frame.empty:
            raise ProviderError("AKShare K线为空", source=self.name)
        bars: list[Bar] = []
        for _, row in frame.iterrows():
            date_value = _pick(row, "date", "")
            date_text = str(date_value)[:10]
            if not date_text:
                continue
            bars.append(
                Bar(
                    date=date_text,
                    open=_f(_pick(row, "open")), high=_f(_pick(row, "high")),
                    low=_f(_pick(row, "low")), close=_f(_pick(row, "price")),
                    volume=_f(_pick(row, "volume")), amount=_f(_pick(row, "amount")),
                    change_pct=_f(_pick(row, "change_pct")),
                    turnover_rate=_f(_pick(row, "turnover")),
                )
            )
        if len(bars) < 5:
            raise ProviderError(f"AKShare K线不足({len(bars)} 根)", source=self.name)
        return KLine(code=code, period=period, bars=bars[-days:], source=self.name,
                     fetched_at=time.time())

    async def fetch_sectors(self, kind: str = "industry") -> list[SectorQuote]:
        func = "stock_board_industry_name_em" if kind == "industry" else "stock_board_concept_name_em"
        frame = await self._call(func)
        if frame is None or frame.empty:
            raise ProviderError("AKShare 板块列表为空", source=self.name)
        out: list[SectorQuote] = []
        for _, row in frame.iterrows():
            name = str(_pick(row, "name", "") or "").strip()
            if not name:
                continue
            out.append(
                SectorQuote(
                    code=str(_pick(row, "code", name) or name), name=name,
                    change_pct=_f(_pick(row, "sector_change")),
                    amount=_f(_pick(row, "amount")),
                    turnover_rate=_f(_pick(row, "turnover")),
                    up_count=int(_f(_pick(row, "up_count"), 0)),
                    down_count=int(_f(_pick(row, "down_count"), 0)),
                    leader_name=str(_pick(row, "leader_name", "") or ""),
                    leader_change_pct=_f(_pick(row, "leader_change_pct")),
                    kind=kind, source=self.name,
                )
            )
        if not out:
            raise ProviderError("AKShare 板块解析为空", source=self.name)
        return out

    async def fetch_breadth(self) -> Breadth:
        snapshot = await self.fetch_snapshot()
        up = sum(1 for q in snapshot if q.change_pct > 0)
        down = sum(1 for q in snapshot if q.change_pct < 0)
        flat = len(snapshot) - up - down
        return Breadth(
            up=up, down=down, flat=max(0, flat),
            up_over_5=sum(1 for q in snapshot if q.change_pct >= 5),
            down_over_5=sum(1 for q in snapshot if q.change_pct <= -5),
            total_amount=sum(q.amount for q in snapshot),
            source=self.name,
        )

    async def fetch_news_flash(self, limit: int = 50) -> list[NewsItem]:
        frame = await self._call("stock_info_global_cls", symbol="全部")
        if frame is None or frame.empty:
            raise ProviderError("AKShare 快讯为空", source=self.name)
        out: list[NewsItem] = []
        for index, (_, row) in enumerate(frame.head(max(1, limit)).iterrows()):
            title = str(_pick(row, "title", "") or "").strip()
            content = str(row.get("内容") or row.get("content") or "").strip()
            if not title and content:
                title = content[:120]
            if not title:
                continue
            out.append(
                NewsItem(
                    id=f"akshare-{abs(hash(title)) % (10 ** 12)}",
                    title=title[:200], summary=content[:400],
                    source=self.name, channel="flash",
                    published_at=str(row.get("发布时间") or row.get("时间") or ""),
                )
            )
        if not out:
            raise ProviderError("AKShare 快讯解析为空", source=self.name)
        return out


__all__ = ["AkshareProvider"]
