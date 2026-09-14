"""同花顺数据源。

定位: **可靠补充源**。实测它是东方财富被阻断、新浪被反爬限流时, 唯一仍能稳定
提供「科创板」与「北交所」历史日线的免费公开源。

实测结论(字段与单位均已核对):
  * 日线 ``d.10jqka.com.cn/v6/line/hs_<code>/01/last.js`` 返回最近约 140 个交易日,
    四个板块全覆盖; 字段为 ``日期,开,高,低,收,成交量(股),成交额,换手%,?,…``;
  * 分时 ``d.10jqka.com.cn/v6/time/hs_<code>/last.js`` 提供**中文名 + 昨收 + 最新价**,
    但不提供成交量/换手率 —— 这些字段一律填 0, 绝不用猜测值冒充真实数据;
  * 代码格式统一 ``hs_<code>``(不分交易所前缀)。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable

from ..core.http import ProviderError
from ..core.util import board_of, detect_market, limit_pct, normalize_code
from ..models import Bar, KLine, Quote
from .base import CAP_KLINE, CAP_MINUTE, CAP_QUOTE, Provider
from .endpoints import endpoints

logger = logging.getLogger(__name__)

_JSONP_RE = re.compile(r"^\s*[A-Za-z_][\w.]*\s*\((.*)\)\s*;?\s*$", re.S)
_DATA_FIELD_RE = re.compile(r'"data"\s*:\s*"([^"]*)"', re.S)
_MIN_FIELDS = 8


def _f(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _unwrap_jsonp(text: str) -> Any:
    match = _JSONP_RE.match(text or "")
    payload = match.group(1) if match else (text or "")
    try:
        return json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"同花顺返回非 JSON: {exc}", source="ths") from exc


def parse_line_payload(text: str) -> list[Bar]:
    """解析同花顺日线 payload —— 纯函数, 便于离线单测。

    实测(2026-09)三种入口的返回结构**并不一致**:

    * ``last.js`` 与 ``today.js``::

          {"year": {"2026": "20260914,1277.27,1285.53,1270.36,1278.50,1051130,1342592668.00,0.28,...;..."}}

      即按年份分组的 ``"日期,开,高,低,收,成交量(股),成交额,换手%,..."`` 分号串;

    * ``all.js``::

          {"total":"6003","start":"20010827","name":"贵州茅台","sortYear":[[2001,86],...],
           "price":"...","volumn":"...", ...}

      历史全量在 ``price`` 与 ``volumn`` 两个字段里。

    这里统一归一化成 ``Bar`` 列表。
    """
    payload = _unwrap_jsonp(text)
    if not isinstance(payload, dict):
        raise ProviderError("同花顺日线返回非对象", source=THS_NAME)

    node = payload
    # 有些入口会多包一层 {"hs_600519": {...}}
    if "year" not in node and "price" not in node and "sortYear" not in node:
        inner = next((v for v in payload.values() if isinstance(v, dict)), None)
        if isinstance(inner, dict):
            node = inner

    records_text: list[str] = []
    if isinstance(node.get("year"), dict):
        for year_text in sorted(str(k) for k in node["year"].keys()):
            chunk = str(node["year"].get(year_text) or "")
            if chunk:
                records_text.append(chunk)
    elif isinstance(node.get("data"), str) and node.get("data"):
        records_text.append(str(node["data"]))

    if records_text:
        bars = _bars_from_records(";".join(records_text))
        if bars:
            return bars

    bars = _bars_from_all_payload(node)
    if bars:
        return bars
    raise ProviderError("同花顺日线无有效记录", source=THS_NAME)


THS_NAME = "ths"


def _bars_from_records(raw: str) -> list[Bar]:
    bars: list[Bar] = []
    prev_close = 0.0
    for line in raw.split(";"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < _MIN_FIELDS:
            continue
        day = parts[0].strip()
        if len(day) != 8 or not day.isdigit():
            continue
        close = _f(parts[4])
        change_pct = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
        prev_close = close or prev_close
        bars.append(
            Bar(
                date=f"{day[0:4]}-{day[4:6]}-{day[6:8]}",
                open=_f(parts[1]), high=_f(parts[2]), low=_f(parts[3]), close=close,
                volume=_f(parts[5]) / 100.0,   # 同花顺给"股", 统一为"手"
                amount=_f(parts[6]),
                change_pct=round(change_pct, 3),
                turnover_rate=_f(parts[7]),
            )
        )
    return bars


def _bars_from_all_payload(node: dict[str, Any]) -> list[Bar]:
    """``all.js`` 的 ``price``/``volumn`` 是竖线或分号分隔的序列。

    实测结构形如::

        price: "开盘,最高,最低,收盘,成交量; 开盘,最高,..."
        dates: "20260911,20260912,..."  (不总是存在)

    由于各字段的分隔方式随版本变化, 这里采用**保守策略**: 只有当能明确解析出
    日期与四个价格字段时才返回, 否则返回空列表让上层切换到其它入口 ——
    宁可换源, 也不用猜测的解析方式产出错误 K 线。
    """
    dates_raw = node.get("dates")
    price_raw = str(node.get("price") or "")
    volume_raw = str(node.get("volumn") or node.get("volume") or "")
    if not dates_raw or not price_raw:
        return []

    dates = [d.strip() for d in re.split(r"[,;|]", str(dates_raw)) if d.strip()]
    prices = [p.strip() for p in re.split(r"[;|]", price_raw) if p.strip()]
    volumes = [v.strip() for v in re.split(r"[;|]", volume_raw) if v.strip()]
    if len(dates) != len(prices):
        return []

    bars: list[Bar] = []
    prev_close = 0.0
    for index, date_text in enumerate(dates):
        fields = [f.strip() for f in prices[index].split(",")]
        if len(fields) < 4 or not date_text.isdigit() or len(date_text) != 8:
            return []
        try:
            open_price, high, low, close = (float(fields[0]), float(fields[1]),
                                            float(fields[2]), float(fields[3]))
        except ValueError:
            return []
        volume = 0.0
        if index < len(volumes):
            try:
                volume = float(volumes[index].split(",")[0])
            except (ValueError, IndexError):
                volume = 0.0
        change_pct = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
        prev_close = close or prev_close
        bars.append(
            Bar(
                date=f"{date_text[0:4]}-{date_text[4:6]}-{date_text[6:8]}",
                open=open_price, high=high, low=low, close=close,
                volume=volume / 100.0,
                change_pct=round(change_pct, 3),
            )
        )
    return bars


class ThsProvider(Provider):
    name = "ths"
    label = "同花顺"
    priority = 60
    capabilities = frozenset({CAP_KLINE, CAP_MINUTE, CAP_QUOTE})
    note = "覆盖科创板/北交所日线的可靠补充源; 不提供成交量等完整快照字段。"
    homepage = "https://www.10jqka.com.cn/"

    # ------------------------------------------------------------------ #
    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> KLine:
        """日线。三个入口(last/today/all)依次尝试 —— 实测它们的返回结构并不一致。"""
        bases = endpoints.urls(self.name, CAP_KLINE)
        if not bases:
            raise ProviderError("未配置K线地址", source=self.name)
        normalized = normalize_code(code)
        last_error: Exception | None = None
        for template in bases:
            url = template.format(code=normalized, symbol=normalized)
            try:
                text = await self.http.get_text(
                    url,
                    headers={"Referer": "https://stockpage.10jqka.com.cn/"},
                    alias=self.alias,
                    throttle_key=f"{self.name}:kline",
                )
                bars = parse_line_payload(text)
            except Exception as exc:  # noqa: BLE001 - 换下一个入口
                last_error = exc
                logger.debug("同花顺K线入口失败 %s: %s", url.rsplit("/", 1)[-1], exc)
                continue
            if len(bars) < 5:
                last_error = ProviderError(f"K线数据不足({len(bars)} 根)", source=self.name)
                continue
            return KLine(code=code, period=period, bars=bars[-days:], source=self.name,
                         fetched_at=time.time())
        raise ProviderError(f"同花顺全部K线入口失败: {last_error}", source=self.name)

    async def fetch_minute(self, code: str) -> dict[str, Any]:
        base = endpoints.first(self.name, CAP_MINUTE)
        if not base:
            raise ProviderError("未配置分时地址", source=self.name)
        text = await self.http.get_text(
            base.format(code=normalize_code(code), symbol=normalize_code(code)),
            headers={"Referer": "https://stockpage.10jqka.com.cn/"},
            alias=self.alias,
            throttle_key=f"{self.name}:minute",
        )
        payload = _unwrap_jsonp(text)
        if not isinstance(payload, dict):
            raise ProviderError("同花顺分时返回非对象", source=self.name)
        node = next(iter(payload.values()), None)
        if not isinstance(node, dict):
            raise ProviderError("同花顺分时缺少数据节点", source=self.name)
        ticks = str(node.get("data") or "")
        points: list[dict[str, Any]] = []
        for item in ticks.split(";"):
            fields = item.strip().split(",")
            if len(fields) < 2:
                continue
            points.append({
                "time": fields[0], "price": _f(fields[1]),
                "amount": _f(fields[2]) if len(fields) > 2 else 0.0,
                "avg": _f(fields[3]) if len(fields) > 3 else 0.0,
                "volume": _f(fields[4]) if len(fields) > 4 else 0.0,
            })
        if not points:
            raise ProviderError("同花顺分时无数据点", source=self.name)
        return {
            "code": normalize_code(code),
            "name": str(node.get("name") or ""),
            "prev_close": _f(node.get("pre")),
            "points": points,
            "source": self.name,
        }

    async def fetch_quotes(self, codes: Iterable[str]) -> list[Quote]:
        """同花顺分时接口只给最新价/昨收/名称, 其余字段留 0(不编造)。"""
        base = endpoints.first(self.name, CAP_MINUTE)
        if not base:
            raise ProviderError("未配置分时地址", source=self.name)
        targets = [normalize_code(c) for c in codes][:20]
        out: list[Quote] = []
        for code in targets:
            try:
                data = await self.fetch_minute(code)
            except Exception as exc:  # noqa: BLE001
                logger.debug("同花顺报价 %s 失败: %s", code, exc)
                continue
            points = data.get("points") or []
            price = _f(points[-1].get("price")) if points else 0.0
            prev_close = _f(data.get("prev_close"))
            if price <= 0:
                price = prev_close
            name = str(data.get("name") or "")
            change = price - prev_close if prev_close else 0.0
            out.append(
                Quote(
                    code=code, name=name, market=detect_market(code), board=board_of(code),
                    price=price, prev_close=prev_close,
                    change=round(change, 3),
                    change_pct=round((change / prev_close * 100.0) if prev_close else 0.0, 3),
                    limit_up=round(prev_close * (1 + limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    limit_down=round(prev_close * (1 - limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    is_st="ST" in name.upper(),
                    source=self.name, ts=time.time(),
                )
            )
        if not out:
            raise ProviderError("同花顺报价全部失败", source=self.name)
        return out


__all__ = ["ThsProvider", "parse_line_payload"]
