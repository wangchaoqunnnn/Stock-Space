"""新浪财经数据源。

覆盖能力: 全市场代码表/快照 / 实时行情 / 日K / 快讯。

新浪的价值在于**沪深京 A 股列表最完整**(用 ``node=hs_a`` 一次即可拿到含科创板与
北交所的 5000+ 只), 因此它是 ``code_list`` 的首选源。代价是反爬敏感:
高频请求会返回 HTTP 456 并封禁约 10 分钟, 所以:
  * 全市场列表建议一天只抓一次(由上层 ``snapshot`` 缓存与磁盘 blob 缓存保证);
  * 本模块的请求间隔由 ``HttpClient.min_interval`` 统一排队;
  * 收到 456 时 ``HttpClient`` 会把该 host 标记冷却, 避免"越试越封"。

实时行情返回 ``var hq_str_sh600519="贵州茅台,1680.00,...";`` 形式的 GBK 文本。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

from ..core.http import ProviderError
from ..core.util import board_of, detect_market, limit_pct, normalize_code, sina_symbol, to_symbol
from ..models import Bar, KLine, MoneyFlow, NewsItem, Quote
from .base import (
    CAP_CODE_LIST,
    CAP_KLINE,
    CAP_MONEY_FLOW,
    CAP_NEWS_FLASH,
    CAP_QUOTE,
    CAP_SNAPSHOT,
    Provider,
)
from .endpoints import endpoints

logger = logging.getLogger(__name__)

BATCH_SIZE = 40

_HQ_RE = re.compile(r'var hq_str_([a-z]{2})(\d{6})="([^"]*)";')

#: 全市场节点(按覆盖度从高到低)
_NODES = ("hs_a", "sh_a", "sz_a")


def _f(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SinaProvider(Provider):
    name = "sina"
    label = "新浪财经"
    priority = 80
    capabilities = frozenset({
        CAP_SNAPSHOT, CAP_QUOTE, CAP_KLINE, CAP_CODE_LIST, CAP_NEWS_FLASH, CAP_MONEY_FLOW,
    })
    note = "沪深京 A 股列表最完整(含科创板/北交所); 高频请求会被 456 限流, 已内置冷却。"
    homepage = "https://finance.sina.com.cn/"

    # ------------------------------------------------------------------ #
    # 列表 / 快照
    # ------------------------------------------------------------------ #
    async def fetch_snapshot(self) -> list[Quote]:
        quotes = await self._fetch_node_list()
        if not quotes:
            raise ProviderError("列表返回空结果", source=self.name)
        return quotes

    async def fetch_code_list(self) -> list[dict[str, Any]]:
        quotes = await self._fetch_node_list()
        if not quotes:
            raise ProviderError("代码表返回空结果", source=self.name)
        return [
            {"code": q.code, "name": q.name, "market": q.market, "board": q.board}
            for q in quotes
        ]

    async def _fetch_node_list(self) -> list[Quote]:
        base = endpoints.first(self.name, CAP_SNAPSHOT)
        if not base:
            raise ProviderError("未配置列表地址", source=self.name)

        collected: dict[str, Quote] = {}
        last_error: Exception | None = None
        for node in _NODES:
            page = 1
            while page <= 60:
                try:
                    payload = await self.http.get_json(
                        base,
                        params={
                            "page": page, "num": 100, "sort": "symbol", "asc": 1,
                            "node": node, "symbol": "", "_s_r_a": "page",
                        },
                        headers={"Referer": "https://finance.sina.com.cn/"},
                        alias=self.alias,
                        throttle_key=f"{self.name}:list",
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    break
                rows = payload if isinstance(payload, list) else []
                if not rows:
                    break
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    code = str(row.get("code") or "").strip()
                    if not re.fullmatch(r"\d{6}", code):
                        continue
                    if code in collected:
                        continue
                    collected[code] = self._row_to_quote(row)
                if len(rows) < 100:
                    break
                page += 1
            if len(collected) > 3000:
                break  # 已经在 hs_a 拿到全量, 不必再试其它节点

        if not collected:
            if last_error:
                raise ProviderError(f"列表获取失败: {last_error}", source=self.name)
            raise ProviderError("列表返回空结果", source=self.name)
        return list(collected.values())

    def _row_to_quote(self, row: dict[str, Any]) -> Quote:
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        prev_close = _f(row.get("settlement"))
        price = _f(row.get("trade"))
        change_pct = _f(row.get("changepercent"))
        if price <= 0 and prev_close > 0:
            price = prev_close
        return Quote(
            code=code,
            name=name,
            market=detect_market(code),
            board=board_of(code),
            price=price,
            prev_close=prev_close,
            open=_f(row.get("open")),
            high=_f(row.get("high")),
            low=_f(row.get("low")),
            change=_f(row.get("pricechange")),
            change_pct=change_pct,
            volume=_f(row.get("volume")),
            amount=_f(row.get("amount")),
            turnover_rate=_f(row.get("turnoverratio")),
            pe_ttm=_f(row.get("per")),
            pb=_f(row.get("pb")),
            total_mv=_f(row.get("mktcap")) * 10000.0,      # 新浪单位: 万元
            float_mv=_f(row.get("nmc")) * 10000.0,
            limit_up=round(prev_close * (1 + limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
            limit_down=round(prev_close * (1 - limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
            is_st="ST" in name.upper(),
            source=self.name,
            ts=time.time(),
        )

    # ------------------------------------------------------------------ #
    # 实时行情
    # ------------------------------------------------------------------ #
    async def fetch_quotes(self, codes: Iterable[str]) -> list[Quote]:
        base = endpoints.first(self.name, CAP_QUOTE)
        if not base:
            raise ProviderError("未配置行情地址", source=self.name)
        targets = [normalize_code(c) for c in codes]
        if not targets:
            return []

        quotes: list[Quote] = []
        for start in range(0, len(targets), BATCH_SIZE):
            chunk = targets[start:start + BATCH_SIZE]
            query = ",".join(sina_symbol(code) for code in chunk)
            text = await self.http.get_text(
                f"{base}{query}",
                headers={"Referer": "https://finance.sina.com.cn/"},
                alias=self.alias,
                throttle_key=f"{self.name}:hq",
                encoding="gb18030",
            )
            quotes.extend(self._parse_hq(text))
        if not quotes:
            raise ProviderError("行情返回空结果", source=self.name)
        return quotes

    def _parse_hq(self, text: str) -> list[Quote]:
        out: list[Quote] = []
        for match in _HQ_RE.finditer(text or ""):
            market, code, payload = match.group(1), match.group(2), match.group(3)
            parts = payload.split(",")
            if len(parts) < 32:
                continue
            name = parts[0].strip()
            if not name:
                continue
            open_price = _f(parts[1])
            prev_close = _f(parts[2])
            price = _f(parts[3])
            if price <= 0:
                price = prev_close
            change = price - prev_close if prev_close else 0.0
            change_pct = (change / prev_close * 100.0) if prev_close else 0.0
            high = _f(parts[4])
            low = _f(parts[5])
            amplitude = ((high - low) / prev_close * 100.0) if prev_close else 0.0
            out.append(
                Quote(
                    code=code, name=name, market=market, board=board_of(code),
                    price=price, prev_close=prev_close, open=open_price,
                    high=high, low=low, change=round(change, 3), change_pct=round(change_pct, 3),
                    volume=_f(parts[8]), amount=_f(parts[9]),
                    amplitude=round(amplitude, 3),
                    limit_up=round(prev_close * (1 + limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    limit_down=round(prev_close * (1 - limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    is_st="ST" in name.upper(),
                    source=self.name, ts=time.time(),
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # K 线
    # ------------------------------------------------------------------ #
    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> KLine:
        base = endpoints.first(self.name, CAP_KLINE)
        if not base:
            raise ProviderError("未配置K线地址", source=self.name)
        scale = {"day": 240, "week": 1200, "month": 5200}.get(period, 240)
        payload = await self.http.get_json(
            base,
            params={
                "symbol": to_symbol(code),
                "scale": scale,
                "ma": "no",
                "datalen": max(30, min(1023, days)),
            },
            headers={"Referer": "https://finance.sina.com.cn/"},
            alias=self.alias,
            throttle_key=f"{self.name}:kline",
        )
        rows = payload if isinstance(payload, list) else []
        bars: list[Bar] = []
        prev_close = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            close = _f(row.get("close"))
            change_pct = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
            prev_close = close or prev_close
            bars.append(
                Bar(
                    date=str(row.get("day") or row.get("date") or "")[:10],
                    open=_f(row.get("open")), high=_f(row.get("high")),
                    low=_f(row.get("low")), close=close,
                    volume=_f(row.get("volume")),
                    amount=_f(row.get("amount")),
                    change_pct=round(change_pct, 3),
                )
            )
        if len(bars) < 5:
            raise ProviderError(f"K线数据不足({len(bars)} 根)", source=self.name)
        return KLine(code=code, period=period, bars=bars, source=self.name, fetched_at=time.time())

    # ------------------------------------------------------------------ #
    # 7×24 快讯
    # ------------------------------------------------------------------ #
    async def fetch_news_flash(self, limit: int = 50) -> list[NewsItem]:
        """新浪财经 7×24 快讯。

        响应结构为 ``{"result": {"data": {"feed": {"list": [...]}}}}`` ——
        注意正文在 ``rich_text``(带 HTML 标签), 需要剥标签后再入库。
        """
        base = endpoints.first(self.name, CAP_NEWS_FLASH)
        if not base:
            raise ProviderError("未配置快讯地址", source=self.name)
        payload = await self.http.get_json(
            base,
            params={"page": 1, "page_size": max(10, min(100, limit)), "zhibo_id": 152,
                    "tag_id": 0, "dire": "f", "dpc": 1, "type": 0},
            headers={"Referer": "https://finance.sina.com.cn/7x24/"},
            alias=self.alias,
            throttle_key=f"{self.name}:news",
        )
        data = (payload or {}).get("result") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ProviderError("快讯返回结构异常", source=self.name)
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        feed = inner.get("feed") if isinstance(inner, dict) else None
        rows = (feed or {}).get("list") if isinstance(feed, dict) else None
        if not isinstance(rows, list) or not rows:
            # 回退: 有些时段 feed 为空但 zhibo 有内容
            rows = inner.get("zhibo") if isinstance(inner, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ProviderError("快讯列表为空(该时段可能无更新)", source=self.name)

        out: list[NewsItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("rich_text") or row.get("text") or "").strip()
            text = _strip_tags(raw)
            if not text:
                continue
            out.append(
                NewsItem(
                    id=f"sina-{row.get('id') or abs(hash(text)) % (10 ** 12)}",
                    title=text[:120],
                    summary=text[:400],
                    url=str(row.get("docurl") or row.get("url") or ""),
                    source=self.name,
                    channel="flash",
                    published_at=str(row.get("create_time") or row.get("update_time") or ""),
                    related_codes=_extract_codes(text),
                )
            )
        if not out:
            raise ProviderError("快讯解析为空", source=self.name)
        return out


    # ------------------------------------------------------------------ #
    # 个股资金流
    # ------------------------------------------------------------------ #
    async def fetch_money_flow(self, code: str) -> MoneyFlow:
        """新浪个股资金流（日度口径）。

        来源: 92KeBi ``real/moneyflow.py``。URL 形如
        ``MoneyFlow.ssl_qsfx_zjlrqs?daima=sh600519``，返回**按日数组**（最新在前）。

        ⚠️ 三个必须记住的口径细节：

        1. **单位是元**，而东财 push2 fflow 返回的是**万元** —— 本方法负责换算成与
           东财一致的"万元"，否则同一个字段在两源之间会差 10000 倍。
           实测校验（600519, 2026-09-11）：``|netamount/ratioamount|`` = 43.56 亿，
           与"换手 27.37 万手 × 1276.5 元 ≈ 34.9 亿"同量级 —— 只有按元解释才成立。
        2. **只有日度数据**：最新一条通常是**上一交易日**（盘后更新），东财则是盘中实时。
           因此本源适合作兜底，不适合作首选。
        3. 分级口径与东财不同：这里只有 ``netamount``(主力净额) 与 ``r0_net``(超大单)。
           ``large_net`` 由 ``netamount - r0_net`` 推导（即"主力中扣掉超大单的部分"），
           **不伪造** medium/small —— 这两个该接口确实没有，保持 0 而不是编一个值。
        """
        url = endpoints.first(self.name, CAP_MONEY_FLOW)
        if not url:
            raise ProviderError("未配置资金流地址", source=self.name)
        payload = await self.http.get_json(
            url,
            params={"daima": sina_symbol(code)},
            headers={"Referer": "https://finance.sina.com.cn/"},
            alias=self.alias,
            throttle_key=f"{self.name}:flow",
        )
        rows = payload if isinstance(payload, list) else []
        if not rows:
            raise ProviderError("资金流返回空数据", source=self.name)

        latest = next((r for r in rows if isinstance(r, dict)), None)
        if latest is None:
            raise ProviderError("资金流返回结构异常", source=self.name)

        #: 接口按最新在前返回；取第一条即最近交易日
        main_net = _f(latest.get("netamount"))        # 元 -> 万元
        super_net = _f(latest.get("r0_net"))
        ratio = _f(latest.get("ratioamount"))
        if ratio == 0 and main_net == 0:
            raise ProviderError("资金流最新记录为空值", source=self.name)

        return MoneyFlow(
            code=code,
            name="",
            main_net=main_net / 1e4,
            main_net_pct=ratio * 100.0,
            super_net=super_net / 1e4,
            large_net=(main_net - super_net) / 1e4,
            medium_net=0.0,
            small_net=0.0,
            source=self.name,
        )


_TAG_RE = re.compile(r"<[^>]+>")
_CODE_RE = re.compile(r"(?<!\d)((?:00|30|60|68|8[3-9]|43|92)\d{4})(?!\d)")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").replace("&nbsp;", " ").strip()


def _extract_codes(text: str) -> list[str]:
    return list(dict.fromkeys(_CODE_RE.findall(text or "")))[:10]


__all__ = ["SinaProvider"]
