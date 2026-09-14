"""腾讯财经数据源。

覆盖能力: 批量实时行情 / 日周月K线 / 分时 / 快照 / 板块列表。

腾讯的强项是**批量实时行情**(一次请求 60 只, 全市场约 100 次请求即可刷完),
以及稳定可靠的前复权日 K; 弱项是排行榜不含科创板, 因此 ``rank`` 不声明为本源能力。

字段解析基于 ``v_sh600519="1~贵州茅台~600519~...";`` 的分号分隔文本格式。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

from ..core.http import ProviderError
from ..core.util import board_of, detect_market, limit_pct, normalize_code, tencent_symbol
from ..models import Bar, KLine, Quote, SectorQuote
from .base import (
    CAP_KLINE,
    CAP_MINUTE,
    CAP_QUOTE,
    CAP_SECTOR,
    CAP_SECTOR_MEMBERS,
    CAP_SNAPSHOT,
    Provider,
)
from .endpoints import endpoints

logger = logging.getLogger(__name__)

#: 腾讯单次请求最大代码数(实测 60 稳定, 100 会偶发截断)
BATCH_SIZE = 60

_LINE_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')


def _f(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class TencentProvider(Provider):
    name = "tencent"
    label = "腾讯财经"
    priority = 70
    capabilities = frozenset(
        {CAP_QUOTE, CAP_SNAPSHOT, CAP_KLINE, CAP_MINUTE, CAP_SECTOR, CAP_SECTOR_MEMBERS}
    )
    note = "批量实时行情与日K很稳, 支持一次请求 60 只; 排行榜不含科创板, 所以不参与榜单能力。"
    homepage = "https://gu.qq.com/"

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
            query = ",".join(tencent_symbol(code) for code in chunk)
            text = await self.http.get_text(
                f"{base}{query}",
                headers={"Referer": "https://gu.qq.com/"},
                alias=self.alias,
                throttle_key=f"{self.name}:quote",
                encoding="gb18030",
            )
            quotes.extend(self._parse_batch(text))
        if not quotes:
            raise ProviderError("行情返回空结果", source=self.name)
        return quotes

    def _parse_batch(self, text: str) -> list[Quote]:
        out: list[Quote] = []
        for match in _LINE_RE.finditer(text or ""):
            symbol, payload = match.group(1), match.group(2)
            parts = payload.split("~")
            if len(parts) < 40:
                continue
            code = parts[2].strip() if len(parts) > 2 else symbol[2:]
            if not re.fullmatch(r"\d{6}", code):
                continue
            name = parts[1].strip()
            # 腾讯字段单位: volume=手, amount=元, 市值=亿元;
            # 平台统一口径: 成交量=股、成交额=元、市值=元。
            out.append(
                Quote(
                    code=code,
                    name=name,
                    market=symbol[:2],
                    board=board_of(code),
                    price=_f(parts[3]),
                    prev_close=_f(parts[4]),
                    open=_f(parts[5]),
                    volume=_f(parts[6]) * 100.0,
                    high=_f(parts[33]),
                    low=_f(parts[34]),
                    change=_f(parts[31]),
                    change_pct=_f(parts[32]),
                    amount=_f(parts[37]),
                    turnover_rate=_f(parts[38]) if len(parts) > 38 else 0.0,
                    pe_ttm=_f(parts[39]) if len(parts) > 39 else 0.0,
                    amplitude=_f(parts[43]) if len(parts) > 43 else 0.0,
                    float_mv=_f(parts[44]) * 100000000.0 if len(parts) > 44 else 0.0,
                    total_mv=_f(parts[45]) * 100000000.0 if len(parts) > 45 else 0.0,
                    pb=_f(parts[46]) if len(parts) > 46 else 0.0,
                    limit_up=_f(parts[47]) if len(parts) > 47 else 0.0,
                    limit_down=_f(parts[48]) if len(parts) > 48 else 0.0,
                    volume_ratio=_f(parts[49]) if len(parts) > 49 else 0.0,
                    is_st="ST" in name.upper(),
                    source=self.name,
                    ts=time.time(),
                )
            )
        return out

    async def fetch_snapshot(self) -> list[Quote]:
        """用榜单接口按多个排序维度翻页拼出快照。

        ⚠️ **腾讯没有"一次返回全市场"的接口**, 且它的排行榜不含科创板,
        因此本源在 ``snapshot`` 能力上**天然不完整**, 只作为东财/新浪之后的末位备选 ——
        注册中心的校验器(要求条数 ≥100)会让它在数据不足时被跳过, 而不是把残缺数据当完整数据用。

        (这里刻意**不**去回调注册中心的 snapshot, 否则会在"腾讯自己就是候选"时造成递归。)
        """
        base = endpoints.first(self.name, CAP_SNAPSHOT)
        if not base:
            raise ProviderError("未配置快照地址", source=self.name)
        quotes: list[Quote] = []
        seen: set[str] = set()
        for sort_field in ("turnover", "price", "zdf", "volume", "hsl"):
            for page in range(3):
                try:
                    payload = await self.http.get_json(
                        base,
                        params={"board_code": "aStock", "sort_type": sort_field,
                                "direct": "down", "offset": page * 200, "count": 200},
                        headers={"Referer": "https://gu.qq.com/"},
                        alias=self.alias,
                        throttle_key=f"{self.name}:rank",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("腾讯快照分块失败 %s/%s: %s", sort_field, page, exc)
                    break
                rows = _rank_rows(payload)
                if not rows:
                    break
                for row in rows:
                    code = str(row.get("code") or "").strip()
                    if not re.fullmatch(r"\d{6}", code) or code in seen:
                        continue
                    seen.add(code)
                    quotes.append(_rank_row_to_quote(row, self.name))
                if len(rows) < 200:
                    break
        if len(quotes) < 100:
            raise ProviderError(f"快照条数过少({len(quotes)}), 数据不完整", source=self.name)
        return quotes

    # ------------------------------------------------------------------ #
    # K 线
    # ------------------------------------------------------------------ #
    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> KLine:
        """日/周/月 K 线。

        腾讯有多个等价入口, 但可达性因网络而异(实测 ``web.ifzq.../fqkline`` 返回 501
        而 ``proxy.finance.qq.com/ifzqgtimg/.../newfqkline`` 正常), 因此这里**逐个入口尝试**,
        而不是只用一个入口然后整体换源。
        """
        bases = endpoints.urls(self.name, CAP_KLINE)
        if not bases:
            raise ProviderError("未配置K线地址", source=self.name)
        symbol = tencent_symbol(code)
        kind = {"day": "day", "week": "week", "month": "month"}.get(period, "day")
        params = {
            "param": f"{symbol},{kind},,,{max(30, min(2000, days))},qfq",
            "_var": f"kline_{kind}",
        }
        last_error: Exception | None = None
        for base in bases:
            try:
                payload = await self.http.get_json(
                    base,
                    params=params,
                    headers={"Referer": "https://gu.qq.com/"},
                    alias=self.alias,
                    throttle_key=f"{self.name}:kline",
                )
                return self._parse_kline(payload, symbol, kind, code, period, days)
            except Exception as exc:  # noqa: BLE001 - 换下一个入口
                last_error = exc
                logger.debug("腾讯K线入口失败 %s: %s", base.split("/")[2], exc)
                continue
        raise ProviderError(f"腾讯全部K线入口失败: {last_error}", source=self.name)

    def _parse_kline(self, payload: Any, symbol: str, kind: str,
                     code: str, period: str, days: int) -> KLine:
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        node = (data or {}).get(symbol) if isinstance(data, dict) else None
        if not isinstance(node, dict):
            raise ProviderError("K线返回空数据", source=self.name)

        series = node.get(f"qfq{kind}") or node.get(kind) or []
        bars: list[Bar] = []
        prev_close = 0.0
        for item in series:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            # 腾讯 bar 顺序为 [日期, 开, 收, 高, 低, 成交量(手)] —— 不是 OHLC!
            # 实测交叉验证过, 顺序写错会让最高/最低互换, 直接影响形态判定。
            close = _f(item[2])
            change_pct = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
            prev_close = close or prev_close
            bars.append(
                Bar(
                    date=str(item[0]),
                    open=_f(item[1]), close=close,
                    high=_f(item[3]), low=_f(item[4]),
                    volume=_f(item[5]) * 100.0,  # 腾讯返回"手", 平台统一口径为"股"
                    change_pct=round(change_pct, 3),
                )
            )
        if len(bars) < 5:
            raise ProviderError(f"K线数据不足({len(bars)} 根)", source=self.name)
        return KLine(code=code, period=period, bars=bars[-days:], source=self.name,
                     fetched_at=time.time())

    async def fetch_minute(self, code: str) -> dict[str, Any]:
        base = endpoints.first(self.name, CAP_MINUTE)
        if not base:
            raise ProviderError("未配置分时地址", source=self.name)
        symbol = tencent_symbol(code)
        payload = await self.http.get_json(
            base,
            params={"code": symbol},
            headers={"Referer": "https://gu.qq.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:minute",
        )
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        node = (data or {}).get(symbol) if isinstance(data, dict) else None
        raw = (node or {}).get("data") if isinstance(node, dict) else None
        lines = (raw or {}).get("data") if isinstance(raw, dict) else None
        if not lines:
            raise ProviderError("分时返回空数据", source=self.name)
        points = []
        for line in lines:
            parts = str(line).split(" ")
            if len(parts) < 3:
                continue
            points.append({"time": parts[0], "price": _f(parts[1]), "volume": _f(parts[2])})
        if not points:
            raise ProviderError("分时无数据点", source=self.name)
        prev_close = _f((raw or {}).get("prec") if isinstance(raw, dict) else 0)
        return {"code": code, "prev_close": prev_close, "points": points, "source": self.name}

    # ------------------------------------------------------------------ #
    # 板块
    # ------------------------------------------------------------------ #
    async def fetch_sectors(self, kind: str = "industry") -> list[SectorQuote]:
        base = endpoints.first(self.name, CAP_SECTOR)
        if not base:
            raise ProviderError("未配置板块地址", source=self.name)
        payload = await self.http.get_json(
            base,
            params={"board_type": "hy" if kind == "industry" else "gn",
                    "sort_type": "price", "direct": "down", "offset": 0, "count": 200},
            headers={"Referer": "https://gu.qq.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:rank",
        )
        out: list[SectorQuote] = []
        for row in _rank_rows(payload):
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            out.append(
                SectorQuote(
                    code=str(row.get("code") or row.get("board_code") or name), name=name,
                    change_pct=_f(row.get("zdf") or row.get("change_pct")),
                    amount=_f(row.get("amount") or row.get("cje")),
                    leader_name=str(row.get("leader_name") or row.get("lzg") or ""),
                    kind=kind, source=self.name,
                )
            )
        if not out:
            raise ProviderError("板块返回空结果", source=self.name)
        return out

    async def fetch_sector_members(self, sector_code: str, sector_name: str = "") -> list[Quote]:
        base = endpoints.first(self.name, CAP_SECTOR_MEMBERS)
        if not base:
            raise ProviderError("未配置板块成分地址", source=self.name)
        payload = await self.http.get_json(
            base,
            params={"board_code": sector_code or sector_name, "sort_type": "turnover",
                    "direct": "down", "offset": 0, "count": 200},
            headers={"Referer": "https://gu.qq.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:rank",
        )
        rows = _rank_rows(payload)
        if not rows:
            raise ProviderError("板块成分返回空结果", source=self.name)
        return [_rank_row_to_quote(row, self.name) for row in rows]


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #
def _rank_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("rank_list", "list", "data", "stock_list"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _rank_row_to_quote(row: dict[str, Any], source: str) -> Quote:
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip()
    price = _f(row.get("zxj") or row.get("price") or row.get("now"))
    prev_close = _f(row.get("zss") or row.get("prev_close"))
    return Quote(
        code=code, name=name, market=detect_market(code), board=board_of(code),
        price=price, prev_close=prev_close,
        change_pct=_f(row.get("zdf") or row.get("change_pct")),
        change=_f(row.get("zd") or row.get("change")),
        volume=_f(row.get("volume") or row.get("cjl")),
        amount=_f(row.get("amount") or row.get("cje")),
        turnover_rate=_f(row.get("hsl") or row.get("turnover")),
        limit_up=round(prev_close * (1 + limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
        limit_down=round(prev_close * (1 - limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
        is_st="ST" in name.upper(),
        source=source, ts=time.time(),
    )


__all__ = ["TencentProvider"]
