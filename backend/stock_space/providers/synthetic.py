"""合成数据源(离线演示 / 自动化测试兜底)。

**只在 ``DATA_SOURCES__MODE=synthetic`` 时启用**, 并且所有响应都会带上
``source="synthetic"``; 前端会显著标注「演示数据」。这是刻意的设计:
真实源全挂时宁可明确报错, 也不能把假数据当真实数据展示。

行情剧本是**确定性**的(以证券代码为随机种子), 因此:
  * 同一天多次运行结果一致, 便于回归测试;
  * 价格序列带有趋势 + 噪声, 足以让所有策略产生有意义的信号;
  * 包含涨停/跌停/温和放量/缩量回调等形态, 让各引擎都能被验证到。
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from datetime import date, timedelta
from typing import Any, Iterable

from ..core.util import board_of, detect_market, limit_pct, normalize_code, today_str
from ..models import Bar, Breadth, KLine, LimitUpStock, Quote, SectorQuote
from .base import (
    CAP_BREADTH,
    CAP_CODE_LIST,
    CAP_KLINE,
    CAP_LIMIT_UP_POOL,
    CAP_QUOTE,
    CAP_RANK,
    CAP_SECTOR,
    CAP_SECTOR_MEMBERS,
    CAP_SNAPSHOT,
    Provider,
)

logger = logging.getLogger(__name__)

#: 合成市场板块
_SECTORS = (
    "半导体", "消费电子", "光伏设备", "电池", "汽车整车", "软件开发", "医疗器械",
    "化学制药", "食品饮料", "银行", "证券", "保险", "房地产开发", "工程建设",
    "有色金属", "煤炭开采", "电力", "通信设备", "计算机设备", "航天航空",
)

#: 代码前缀模板(覆盖沪深京四个板块)
_PREFIXES = ("600", "601", "603", "000", "001", "002", "300", "301", "688", "920", "830")

_NAME_CHARS = "华夏天山金鼎泰隆宏远智联星辰盛通新宇乾元"


def _seed_of(code: str) -> int:
    return int(hashlib.md5(code.encode("utf-8")).hexdigest()[:8], 16)


def _trading_days(count: int) -> list[str]:
    """生成最近 ``count`` 个交易日(跳过周末)。"""
    days: list[str] = []
    cursor = date.today()
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return list(reversed(days))


class SyntheticProvider(Provider):
    name = "synthetic"
    label = "内置演示数据"
    priority = 1
    capabilities = frozenset(
        {
            CAP_SNAPSHOT, CAP_QUOTE, CAP_KLINE, CAP_RANK, CAP_SECTOR,
            CAP_SECTOR_MEMBERS, CAP_BREADTH, CAP_LIMIT_UP_POOL, CAP_CODE_LIST,
        }
    )
    note = "确定性合成行情, 仅用于离线演示与自动化测试; 启用后界面会明确标注「演示数据」。"
    homepage = ""

    #: 合成市场规模
    universe_size = 320

    # ------------------------------------------------------------------ #
    # 代码表 / 基础信息
    # ------------------------------------------------------------------ #
    def _universe(self) -> list[tuple[str, str, str]]:
        """确定性生成 ``(code, name, sector)`` 列表(带缓存的模块级结果)。"""
        cached = getattr(self, "_universe_cache", None)
        if cached is not None:
            return cached
        out: list[tuple[str, str, str]] = []
        for index in range(self.universe_size):
            prefix = _PREFIXES[index % len(_PREFIXES)]
            code = f"{prefix}{index:03d}"[:6].ljust(6, "0")
            rng = random.Random(_seed_of(code))
            name = "".join(rng.choice(_NAME_CHARS) for _ in range(2)) + rng.choice("股份科技集团电子医药能源")
            out.append((code, name[:4], _SECTORS[index % len(_SECTORS)]))
        self._universe_cache = out
        return out

    def _bars_for(self, code: str, days: int) -> list[Bar]:
        """确定性日线。

        形态上刻意包含四类样本, 让各策略都能被真正触发(否则冒烟测试会出现
        "全部策略都筛不出东西"从而无法验证算法):
          * 缓慢抬升 + 温和放量(潜涨/趋势策略);
          * 周期性涨停(涨停回调/N字/首板确认);
          * 偶发恐慌放量下跌(恐慌反转形态);
          * 其余为随机游走。
        """
        rng = random.Random(_seed_of(code))
        dates = _trading_days(max(60, days))
        board_limit = limit_pct(code)
        base = 5.0 + rng.random() * 60.0
        drifting = rng.random() < 0.40
        drift = 0.0035 if drifting else rng.uniform(-0.002, 0.002)
        volatility = 0.011 if drifting else 0.019
        price = base
        bars: list[Bar] = []
        for index, day in enumerate(dates):
            open_price = price * (1 + rng.gauss(0, volatility / 3))
            if index >= 20 and index % 17 == 0:
                # 涨停日: 收盘恰好贴住涨停价, 且明显放量
                close = price * (1 + board_limit / 100.0)
                high = close
                low = min(open_price, price) * (1 - rng.random() * 0.01)
                volume = (8e6 + rng.random() * 3e7) * (2.4 + rng.random())
            elif index >= 20 and index % 23 == 0:
                # 恐慌放量下跌日
                close = price * (1 - min(0.09, board_limit / 100.0 * 0.75))
                high = max(open_price, price) * (1 + rng.random() * 0.005)
                low = close * (1 - rng.random() * 0.012)
                volume = (8e6 + rng.random() * 3e7) * (2.8 + rng.random())
            else:
                shock = rng.gauss(0.0, volatility)
                change = max(-board_limit / 100.0, min(board_limit / 100.0, drift + shock))
                close = max(0.5, price * (1 + change))
                high = max(open_price, close) * (1 + abs(rng.gauss(0, volatility / 2)))
                low = min(open_price, close) * (1 - abs(rng.gauss(0, volatility / 2)))
                volume = (8e6 + rng.random() * 4e7) * (1.0 + (0.5 if drifting else 0.0))
            change_pct = (close / price - 1.0) * 100.0 if price else 0.0
            bars.append(
                Bar(
                    date=day,
                    open=round(open_price, 2), high=round(high, 2),
                    low=round(low, 2), close=round(close, 2),
                    volume=round(volume, 0),
                    amount=round(volume * close, 0),
                    change_pct=round(change_pct, 3),
                    turnover_rate=round(1.0 + rng.random() * 6.0, 3),
                )
            )
            price = close
        return bars

    def _quote_from_bars(self, code: str, name: str, sector: str, bars: list[Bar]) -> Quote:
        last = bars[-1]
        prev = bars[-2] if len(bars) > 1 else last
        prev_close = prev.close
        change = last.close - prev_close
        return Quote(
            code=code, name=name, market=detect_market(code), board=board_of(code),
            price=last.close, prev_close=prev_close, open=last.open,
            high=last.high, low=last.low,
            change=round(change, 3),
            change_pct=round((change / prev_close * 100.0) if prev_close else 0.0, 3),
            volume=last.volume, amount=last.amount,
            turnover_rate=last.turnover_rate,
            volume_ratio=round(0.8 + abs(change) * 3, 2),
            amplitude=round((last.high - last.low) / prev_close * 100.0, 3) if prev_close else 0.0,
            pe_ttm=round(8 + (abs(_seed_of(code)) % 4000) / 100.0, 2),
            pb=round(0.8 + (abs(_seed_of(code)) % 600) / 100.0, 2),
            total_mv=round(last.close * (2e8 + (_seed_of(code) % 100) * 1e7), 0),
            float_mv=round(last.close * (1e8 + (_seed_of(code) % 60) * 1e7), 0),
            limit_up=round(prev_close * (1 + limit_pct(code, name) / 100.0), 2),
            limit_down=round(prev_close * (1 - limit_pct(code, name) / 100.0), 2),
            is_st=False,
            industry=sector,
            source=self.name,
            ts=time.time(),
        )

    # ------------------------------------------------------------------ #
    async def fetch_snapshot(self) -> list[Quote]:
        start = time.perf_counter()
        quotes = [
            self._quote_from_bars(code, name, sector, self._bars_for(code, 70))
            for code, name, sector in self._universe()
        ]
        logger.debug("合成快照 %d 只, 耗时 %.0fms", len(quotes), (time.perf_counter() - start) * 1000)
        return quotes

    async def fetch_code_list(self) -> list[dict[str, Any]]:
        return [
            {"code": code, "name": name, "market": detect_market(code),
             "board": board_of(code), "industry": sector}
            for code, name, sector in self._universe()
        ]

    async def fetch_quotes(self, codes: Iterable[str]) -> list[Quote]:
        wanted = {normalize_code(c) for c in codes}
        lookup = {code: (name, sector) for code, name, sector in self._universe()}
        out: list[Quote] = []
        for code in wanted:
            name, sector = lookup.get(code, (code, _SECTORS[_seed_of(code) % len(_SECTORS)]))
            out.append(self._quote_from_bars(code, name, sector, self._bars_for(code, 70)))
        if not out:
            raise ValueError("合成行情无匹配代码")
        return out

    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> KLine:
        lookup = {c: (n, s) for c, n, s in self._universe()}
        name = lookup.get(normalize_code(code), (code, ""))[0]
        bars = self._bars_for(normalize_code(code), max(days, 250))
        return KLine(code=normalize_code(code), name=name, period=period,
                     bars=bars[-days:], source=self.name, fetched_at=time.time())

    async def fetch_rank(self, kind: str = "gainers", limit: int = 50) -> list[Quote]:
        quotes = await self.fetch_snapshot()
        if kind == "losers":
            quotes.sort(key=lambda q: q.change_pct)
        elif kind == "amount":
            quotes.sort(key=lambda q: q.amount, reverse=True)
        elif kind == "turnover":
            quotes.sort(key=lambda q: q.turnover_rate, reverse=True)
        else:
            quotes.sort(key=lambda q: q.change_pct, reverse=True)
        return quotes[:limit]

    async def fetch_sectors(self, kind: str = "industry") -> list[SectorQuote]:
        quotes = await self.fetch_snapshot()
        grouped: dict[str, list[Quote]] = {}
        for quote in quotes:
            grouped.setdefault(quote.industry, []).append(quote)
        out: list[SectorQuote] = []
        for name, members in grouped.items():
            change = sum(m.change_pct for m in members) / len(members)
            leader = max(members, key=lambda m: m.change_pct)
            out.append(
                SectorQuote(
                    code=f"BK{abs(_seed_of(name)) % 9000 + 1000}",
                    name=name,
                    change_pct=round(change, 3),
                    amount=sum(m.amount for m in members),
                    up_count=sum(1 for m in members if m.change_pct > 0),
                    down_count=sum(1 for m in members if m.change_pct < 0),
                    leader_name=leader.name,
                    leader_change_pct=leader.change_pct,
                    main_net_inflow=round(sum(m.amount for m in members) * (change / 100.0), 0),
                    kind=kind, source=self.name,
                )
            )
        out.sort(key=lambda s: s.change_pct, reverse=True)
        return out

    async def fetch_sector_members(self, sector_code: str, sector_name: str = "") -> list[Quote]:
        quotes = await self.fetch_snapshot()
        members = [q for q in quotes if q.industry == sector_name] if sector_name else []
        if not members:
            members = [q for q in quotes if q.code.startswith(sector_code[:3])]
        return members

    async def fetch_breadth(self) -> Breadth:
        quotes = await self.fetch_snapshot()
        return Breadth(
            up=sum(1 for q in quotes if q.change_pct > 0),
            down=sum(1 for q in quotes if q.change_pct < 0),
            flat=sum(1 for q in quotes if q.change_pct == 0),
            limit_up=sum(1 for q in quotes if q.change_pct >= limit_pct(q.code, q.name) - 0.3),
            limit_down=sum(1 for q in quotes if q.change_pct <= -(limit_pct(q.code, q.name) - 0.3)),
            up_over_5=sum(1 for q in quotes if q.change_pct >= 5),
            down_over_5=sum(1 for q in quotes if q.change_pct <= -5),
            total_amount=sum(q.amount for q in quotes),
            source=self.name,
        )

    async def fetch_limit_up_pool(self) -> dict[str, Any]:
        quotes = await self.fetch_snapshot()
        limit_up: list[LimitUpStock] = []
        broken: list[LimitUpStock] = []
        for quote in quotes:
            threshold = limit_pct(quote.code, quote.name)
            if quote.change_pct >= threshold - 0.3:
                limit_up.append(
                    LimitUpStock(
                        code=quote.code, name=quote.name, price=quote.price,
                        change_pct=quote.change_pct, amount=quote.amount,
                        turnover_rate=quote.turnover_rate,
                        first_limit_time="09:35:00",
                        last_limit_time="10:12:00",
                        consecutive=1 + (abs(_seed_of(quote.code)) % 3),
                        industry=quote.industry, source=self.name,
                    )
                )
            elif quote.high >= quote.prev_close * (1 + threshold / 100.0) - 0.01:
                broken.append(
                    LimitUpStock(
                        code=quote.code, name=quote.name, price=quote.price,
                        change_pct=quote.change_pct, first_limit_time="09:40:00",
                        open_times=1, industry=quote.industry, is_broken=True, source=self.name,
                    )
                )
        return {"limit_up": limit_up, "broken": broken, "date": today_str().replace("-", ""),
                "source": self.name}


__all__ = ["SyntheticProvider"]
