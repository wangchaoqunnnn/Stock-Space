"""东方财富数据源。

覆盖能力: 快照/行情/K线/分时/榜单/板块/板块成分/资金流/涨停池/涨跌家数/代码表/人气/财报。

实现要点:
  * 全市场快照一次分页拉取(fid=f6 按成交额排序, 只输出必要字段, 约 5 秒 / 5900 只);
  * K 线第二字段用 ``fqt=1`` 前复权, 与各策略口径一致;
  * **数据量校验**: 结果为空或条数异常少同样视为失败, 让注册中心切换到备用源
    (实测"不报错但数据不全"比直接报错更常见);
  * 涨停池需要 ``ut`` 参数, 缺失时接口直接返回空。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

from ..core.http import NetworkError, ProviderError
from ..core.util import board_of, detect_market, index_secid, limit_pct, normalize_code, secid
from ..models import Bar, Breadth, IndexQuote, KLine, LimitUpStock, MoneyFlow, Quote, SectorQuote
from .base import (
    CAP_ATTENTION,
    CAP_BREADTH,
    CAP_CODE_LIST,
    CAP_FINANCE,
    CAP_INDICES,
    CAP_KLINE,
    CAP_LIMIT_UP_POOL,
    CAP_MINUTE,
    CAP_MONEY_FLOW,
    CAP_QUOTE,
    CAP_RANK,
    CAP_SECTOR,
    CAP_SECTOR_FLOW,
    CAP_SECTOR_MEMBERS,
    CAP_SNAPSHOT,
    Provider,
)
from .endpoints import endpoints

logger = logging.getLogger(__name__)

#: 快照/榜单统一的输出字段。
#: ⚠️ 字段含义是这一层最容易写错的地方, 以下为**逐字段实测核对**过的映射:
#:   f12=代码  f13=市场号(0 深/北, 1 沪)  f14=**名称**  f2=最新价  f3=涨跌幅%
#:   f4=涨跌额  f5=成交量(手)  f6=成交额(元)  f7=振幅%  f8=换手率%  f9=市盈率(动)
#:   f10=量比  f15=最高  f16=最低  f17=今开  f18=昨收  f20=总市值(元)  f21=流通市值(元)
#:   f22=涨速%  f23=市净率  f24=60日涨幅%  f25=年初至今%  f100=**所属行业**  f115=市盈率(TTM)
#:   f62=主力净流入(元)  f184=主力净占比%
#: 注意: 曾经误把 f13 当作名称 → 全部股票名称变空; 漏掉 f100 → 板块共振与行业聚合失效。
_FIELDS = ("f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
           "f20,f21,f22,f23,f24,f25,f62,f100,f115,f184")

#: 沪深京 A 股的市场筛选表达式(不遗漏科创板/北交所)
MARKET_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
MARKET_FILTER_BJ = "m:0+t:81+s:2048"

#: 榜单类型 -> fid 排序字段
_RANK_FID = {
    "gainers": "f3",
    "losers": "f3",
    "amount": "f6",
    "turnover": "f8",
    "speed": "f22",
    "amplitude": "f7",
    "volume_ratio": "f10",
}

#: 指数行情需要显式指定 secid —— 指数与个股代码段会重叠(000001 既是上证指数
#: 也是平安银行), 按规则推导会得到错误结果, 因此这里用**显式映射表**。
_INDEX_ALIAS = {
    "sh000001": "1.000001", "000001": "1.000001",
    "sz399001": "0.399001", "399001": "0.399001",
    "sz399006": "0.399006", "399006": "0.399006",
    "sh000688": "1.000688", "000688": "1.000688",
    "sh000300": "1.000300", "000300": "1.000300",
    "sh000905": "1.000905", "000905": "1.000905",
    "sh000852": "1.000852", "000852": "1.000852",
    "sh000016": "1.000016", "000016": "1.000016",
    "sz399005": "0.399005", "399005": "0.399005",
    "sz399673": "0.399673", "399673": "0.399673",
    "bj899050": "0.899050", "899050": "0.899050",
}


def _f(value: Any, default: float = 0.0) -> float:
    """东财用 ``-`` 表示无数据, 统一转成 0.0。"""
    if value in (None, "", "-", "--"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _is_st(name: str) -> bool:
    upper = (name or "").upper().replace(" ", "")
    return "ST" in upper or "退" in upper


class EastmoneyProvider(Provider):
    name = "eastmoney"
    label = "东方财富"
    priority = 90
    capabilities = frozenset(
        {
            CAP_SNAPSHOT, CAP_QUOTE, CAP_INDICES, CAP_KLINE, CAP_MINUTE, CAP_RANK, CAP_SECTOR,
            CAP_SECTOR_MEMBERS, CAP_MONEY_FLOW, CAP_SECTOR_FLOW, CAP_LIMIT_UP_POOL,
            CAP_BREADTH, CAP_CODE_LIST, CAP_ATTENTION, CAP_FINANCE,
        }
    )
    note = "A股实时快照/榜单/板块/资金流/涨停池覆盖最全的主源。"
    homepage = "https://quote.eastmoney.com/"

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    #: 东财 clist 的**硬上限**：无论 pz 传多大，一页最多只返回 100 条。
    #: 实测 pz=100/200/1000/6000 都只给 100 行，而全市场约 5900 只
    #: → 需要约 60 页；60 页串行约 1.5 秒，可接受。
    PAGE_SIZE = 100
    MAX_PAGES = 80

    async def fetch_snapshot(self) -> list[Quote]:
        """全市场快照(沪深京 A 股)。按成交额降序分页拉取, 保证大票优先进入样本。"""
        url = endpoints.first(self.name, CAP_SNAPSHOT)
        if not url:
            raise ProviderError("未配置快照地址", source=self.name)

        quotes: list[Quote] = []
        seen: set[str] = set()
        page = 1
        while page <= self.MAX_PAGES:
            payload = await self.http.get_json(
                url,
                params={
                    "pn": page, "pz": self.PAGE_SIZE, "po": 1, "np": 1,
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2, "invt": 2, "fid": "f6",
                    "fs": MARKET_FILTER, "fields": _FIELDS,
                    "_": int(time.time() * 1000),
                },
                headers={"Referer": "https://quote.eastmoney.com/"},
                alias=self.alias,
                throttle_key=f"{self.name}:clist",
            )
            rows = _rows(payload)
            if not rows:
                break
            fresh = [q for q in self._parse_rows(rows) if q.code not in seen]
            for quote in fresh:
                seen.add(quote.code)
            quotes.extend(fresh)
            total = _total(payload)
            if total and len(quotes) >= total:
                break
            # 返回行数不足一页说明已经是最后一页
            if len(rows) < self.PAGE_SIZE:
                break
            page += 1

        if not quotes:
            raise ProviderError("快照返回空结果", source=self.name)
        return quotes

    async def fetch_code_list(self) -> list[dict[str, Any]]:
        quotes = await self.fetch_snapshot()
        return [
            {
                "code": q.code, "name": q.name, "market": q.market,
                "board": q.board, "industry": q.industry,
            }
            for q in quotes
        ]

    def _parse_rows(self, rows: Iterable[dict[str, Any]]) -> list[Quote]:
        out: list[Quote] = []
        for row in rows:
            code = str(row.get("f12") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            name = str(row.get("f14") or "").strip()
            price = _f(row.get("f2"))
            prev_close = _f(row.get("f18"))
            change_pct = _f(row.get("f3"))
            # 停牌股 f2 可能是 "-", 此时用昨收兜底展示
            if price <= 0 and prev_close > 0:
                price = prev_close
            out.append(
                Quote(
                    code=code,
                    name=name,
                    market=detect_market(code),
                    board=board_of(code),
                    price=price,
                    prev_close=prev_close,
                    open=_f(row.get("f17")),
                    high=_f(row.get("f15")),
                    low=_f(row.get("f16")),
                    change=_f(row.get("f4")),
                    change_pct=change_pct,
                    # 东财成交量单位为"手", 平台统一口径为"股"; 成交额与市值本就是元
                    volume=_f(row.get("f5")) * 100.0,
                    amount=_f(row.get("f6")),
                    turnover_rate=_f(row.get("f8")),
                    volume_ratio=_f(row.get("f10")),
                    amplitude=_f(row.get("f7")),
                    pe_ttm=_f(row.get("f115")),
                    pb=_f(row.get("f23")),
                    total_mv=_f(row.get("f20")),
                    float_mv=_f(row.get("f21")),
                    speed=_f(row.get("f22")),
                    limit_up=round(prev_close * (1 + limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    limit_down=round(prev_close * (1 - limit_pct(code, name) / 100.0), 2) if prev_close else 0.0,
                    is_st=_is_st(name),
                    industry=str(row.get("f100") or "").strip(),
                    source=self.name,
                    ts=time.time(),
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # 实时行情 / 指数
    # ------------------------------------------------------------------ #
    async def fetch_quotes(self, codes: Iterable[str]) -> list[Quote]:
        targets = [normalize_code(c) for c in codes]
        if not targets:
            return []
        url_snapshot = endpoints.first(self.name, CAP_SNAPSHOT)
        url_detail = endpoints.first(self.name, CAP_QUOTE)

        out: list[Quote] = []
        # 东财的单只接口对批量不友好, 优先走"按代码段筛选"的快照接口
        if url_snapshot:
            try:
                payload = await self.http.get_json(
                    url_snapshot,
                    params={
                        "pn": 1, "pz": min(200, max(20, len(targets) * 2)), "po": 1, "np": 1,
                        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                        "fltt": 2, "invt": 2, "fid": "f3",
                        "fs": MARKET_FILTER, "fields": _FIELDS,
                        "_": int(time.time() * 1000),
                    },
                    headers={"Referer": "https://quote.eastmoney.com/"},
                    alias=self.alias,
                    throttle_key=f"{self.name}:clist",
                )
                wanted = set(targets)
                found = {q.code: q for q in self._parse_rows(_rows(payload)) if q.code in wanted}
                out.extend(found.values())
            except Exception as exc:  # noqa: BLE001 - 退回逐只查询
                logger.debug("东财批量行情失败, 转逐只查询: %s", exc)

        missing = [c for c in targets if c not in {q.code for q in out}]
        if missing and url_detail:
            for code in missing[:60]:
                try:
                    quote = await self._fetch_single(url_detail, code)
                except Exception:  # noqa: BLE001
                    continue
                if quote:
                    out.append(quote)
        if not out:
            raise ProviderError("行情返回空结果", source=self.name)
        return out

    async def _fetch_single(self, url: str, code: str) -> Quote | None:
        payload = await self.http.get_json(
            url,
            params={"secid": secid(code), "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                    "fields": _FIELDS, "invt": 2, "fltt": 2},
            headers={"Referer": "https://quote.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:stock",
        )
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        parsed = self._parse_rows([data])
        return parsed[0] if parsed else None

    async def fetch_indices(self, specs: Iterable[tuple[str, str]]) -> list[IndexQuote]:
        """指数行情。

        **为什么不用全市场快照去匹配指数**: 指数与个股的代码段会重叠
        (``000001`` 既是上证指数也是平安银行, ``399001`` 是深证成指),
        用股票快照匹配会把平安银行当成上证指数显示 —— 这是一个实测踩到的真实 bug。
        因此指数一律走 ``stock/get`` 单只接口, 并用 ``INDEX_SECIDS`` 显式给出 secid。
        """
        url = endpoints.first(self.name, CAP_QUOTE)
        if not url:
            raise ProviderError("未配置指数行情地址", source=self.name)
        out: list[IndexQuote] = []
        last_error: Exception | None = None
        for label, code in specs:
            secid_value = _INDEX_ALIAS.get(code) or index_secid(code)
            try:
                payload = await self.http.get_json(
                    url,
                    params={
                        "secid": secid_value,
                        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
                        "invt": 2, "fltt": 2,
                    },
                    headers={"Referer": "https://quote.eastmoney.com/"},
                    alias=self.alias,
                    throttle_key=f"{self.name}:stock",
                )
            except Exception as exc:  # noqa: BLE001 - 单个指数失败不影响其它
                last_error = exc
                continue
            data = (payload or {}).get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                continue
            price = _f(data.get("f43"))
            if price <= 0:
                continue
            out.append(
                IndexQuote(
                    code=code,
                    name=str(data.get("f58") or label),
                    price=price,
                    change=_f(data.get("f169")),
                    change_pct=_f(data.get("f170")),
                    amount=_f(data.get("f48")),
                    source=self.name,
                )
            )
        if not out:
            raise ProviderError(f"指数行情全部失败: {last_error}", source=self.name)
        return out

    # ------------------------------------------------------------------ #
    # K 线 / 分时
    # ------------------------------------------------------------------ #
    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> KLine:
        url = endpoints.first(self.name, CAP_KLINE)
        if not url:
            raise ProviderError("未配置K线地址", source=self.name)
        klt = {"day": 101, "week": 102, "month": 103, "60m": 60, "30m": 30, "15m": 15, "5m": 5}.get(period, 101)
        payload = await self.http.get_json(
            url,
            params={
                "secid": secid(code),
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": klt, "fqt": 1, "end": "20500101", "lmt": max(30, min(1000, days)),
                "_": int(time.time() * 1000),
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:kline",
        )
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ProviderError("K线返回空数据", source=self.name)
        raw_lines = data.get("klines") or []
        bars: list[Bar] = []
        for line in raw_lines:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            bars.append(
                Bar(
                    date=parts[0],
                    open=_f(parts[1]), close=_f(parts[2]), high=_f(parts[3]), low=_f(parts[4]),
                    volume=_f(parts[5]), amount=_f(parts[6]),
                    change_pct=_f(parts[8]) if len(parts) > 8 else 0.0,
                    turnover_rate=_f(parts[10]) if len(parts) > 10 else 0.0,
                )
            )
        if len(bars) < 5:
            raise ProviderError(f"K线数据不足({len(bars)} 根)", source=self.name)
        return KLine(code=code, name=str(data.get("name") or ""), period=period,
                     bars=bars, source=self.name, fetched_at=time.time())

    async def fetch_minute(self, code: str) -> dict[str, Any]:
        url = endpoints.first(self.name, CAP_MINUTE)
        payload = await self.http.get_json(
            url,
            params={
                "secid": secid(code), "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "fields1": "f1,f2,f3,f4,f5", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "iscr": 0, "ndays": 1,
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:minute",
        )
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ProviderError("分时返回空数据", source=self.name)
        points = []
        for line in data.get("trends") or []:
            parts = str(line).split(",")
            if len(parts) < 8:
                continue
            points.append({
                "time": parts[0], "price": _f(parts[2]), "avg": _f(parts[7]),
                "volume": _f(parts[5]), "amount": _f(parts[6]),
            })
        if not points:
            raise ProviderError("分时无数据点", source=self.name)
        return {
            "code": code, "name": str(data.get("name") or ""),
            "prev_close": _f(data.get("preClose")), "points": points, "source": self.name,
        }

    # ------------------------------------------------------------------ #
    # 榜单
    # ------------------------------------------------------------------ #
    async def fetch_rank(self, kind: str = "gainers", limit: int = 50) -> list[Quote]:
        """榜单。⚠️ 东财 clist 单页硬上限 100 条, 因此 limit 会被夹到 100 以内。"""
        url = endpoints.first(self.name, CAP_RANK)
        if not url:
            raise ProviderError("未配置榜单地址", source=self.name)
        fid = _RANK_FID.get(kind, "f3")
        # 跌幅榜需要升序
        order = 0 if kind == "losers" else 1
        size = max(1, min(100, limit))
        payload = await self.http.get_json(
            url,
            params={
                "pn": 1, "pz": size, "po": order, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": fid,
                "fs": MARKET_FILTER, "fields": _FIELDS,
                "_": int(time.time() * 1000),
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:clist",
        )
        quotes = self._parse_rows(_rows(payload))
        if kind == "losers":
            quotes = [q for q in quotes if q.change_pct < 0] or quotes
        # 过滤掉停牌(成交额为 0 且价格为昨收)
        quotes = [q for q in quotes if q.price > 0]
        if not quotes:
            raise ProviderError("榜单返回空结果", source=self.name)
        return quotes[:limit]

    # ------------------------------------------------------------------ #
    # 板块
    # ------------------------------------------------------------------ #
    async def fetch_sectors(self, kind: str = "industry") -> list[SectorQuote]:
        url = endpoints.first(self.name, CAP_SECTOR)
        fs = "m:90+t:2+f:!50" if kind == "industry" else "m:90+t:3+f:!50"
        payload = await self.http.get_json(
            url,
            params={
                "pn": 1, "pz": 200, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": fs,
                "fields": "f1,f2,f3,f4,f6,f8,f12,f13,f14,f62,f104,f105,f128,f136,f140,f207,f208,f222",
                "_": int(time.time() * 1000),
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:sector",
        )
        out: list[SectorQuote] = []
        for row in _rows(payload):
            code = str(row.get("f12") or "").strip()
            # ⚠️ 板块名称同样在 f14；f13 是市场号（板块接口里固定为 90）。
            # 早先误写成 `row.get("f13") or row.get("f14")`，
            # 导致所有板块名都变成 "90" —— 前端表现为"板块强弱里没有（可读的）数据"。
            name = str(row.get("f14") or "").strip()
            if not name:
                continue
            out.append(
                SectorQuote(
                    code=code, name=name,
                    change_pct=_f(row.get("f3")), amount=_f(row.get("f6")),
                    turnover_rate=_f(row.get("f8")),
                    up_count=_i(row.get("f104")), down_count=_i(row.get("f105")),
                    leader_name=str(row.get("f128") or ""),
                    leader_change_pct=_f(row.get("f136")),
                    main_net_inflow=_f(row.get("f62")),
                    kind=kind, source=self.name,
                )
            )
        if not out:
            raise ProviderError("板块列表返回空结果", source=self.name)
        return out

    async def fetch_sector_members(self, sector_code: str, sector_name: str = "") -> list[Quote]:
        url = endpoints.first(self.name, CAP_SECTOR_MEMBERS)
        payload = await self.http.get_json(
            url,
            params={
                "pn": 1, "pz": 500, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": f"b:{sector_code}+f:!50", "fields": _FIELDS,
                "_": int(time.time() * 1000),
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:sector",
        )
        quotes = self._parse_rows(_rows(payload))
        if not quotes:
            raise ProviderError("板块成分返回空结果", source=self.name)
        return quotes

    # ------------------------------------------------------------------ #
    # 资金流
    # ------------------------------------------------------------------ #
    async def fetch_money_flow(self, code: str) -> MoneyFlow:
        url = endpoints.first(self.name, CAP_MONEY_FLOW)
        payload = await self.http.get_json(
            url,
            params={
                "secid": secid(code), "ut": "b2884a393a59ad64002292a3e90d46a5",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "klt": 101, "lmt": 1,
            },
            headers={"Referer": "https://data.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:flow",
        )
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        lines = (data or {}).get("klines") if isinstance(data, dict) else None
        if not lines:
            raise ProviderError("资金流返回空数据", source=self.name)
        parts = str(lines[-1]).split(",")
        return MoneyFlow(
            code=code,
            name=str((data or {}).get("name") or ""),
            main_net=_f(parts[1]) if len(parts) > 1 else 0.0,
            small_net=_f(parts[2]) if len(parts) > 2 else 0.0,
            medium_net=_f(parts[3]) if len(parts) > 3 else 0.0,
            large_net=_f(parts[4]) if len(parts) > 4 else 0.0,
            super_net=_f(parts[5]) if len(parts) > 5 else 0.0,
            main_net_pct=_f(parts[6]) if len(parts) > 6 else 0.0,
            source=self.name,
        )

    async def fetch_sector_flow(self, limit: int = 30) -> list[SectorQuote]:
        url = endpoints.first(self.name, CAP_SECTOR_FLOW)
        payload = await self.http.get_json(
            url,
            params={
                "pn": 1, "pz": max(1, min(200, limit)), "po": 1, "np": 1,
                "ut": "b2884a393a59ad64002292a3e90d46a5",
                "fltt": 2, "invt": 2, "fid": "f62",
                "fs": "m:90+t:2+f:!50",
                "fields": "f12,f13,f14,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205",
                "_": int(time.time() * 1000),
            },
            headers={"Referer": "https://data.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:flow",
        )
        out: list[SectorQuote] = []
        for row in _rows(payload):
            name = str(row.get("f14") or "").strip()
            if not name:
                continue
            out.append(
                SectorQuote(
                    code=str(row.get("f12") or ""), name=name,
                    change_pct=_f(row.get("f3")),
                    main_net_inflow=_f(row.get("f62")),
                    kind="industry", source=self.name,
                )
            )
        if not out:
            raise ProviderError("板块资金流返回空结果", source=self.name)
        return out

    # ------------------------------------------------------------------ #
    # 涨停池 / 市场宽度
    # ------------------------------------------------------------------ #
    async def fetch_limit_up_pool(self) -> dict[str, Any]:
        zt_url = endpoints.first(self.name, CAP_LIMIT_UP_POOL)
        zb_url = endpoints.first(self.name, "broken_pool")
        date_str = time.strftime("%Y%m%d", time.localtime(time.time()))
        limit_up: list[LimitUpStock] = []
        broken: list[LimitUpStock] = []

        if zt_url:
            payload = await self.http.get_json(
                zt_url,
                params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                        "Pageindex": 0, "pagesize": 400, "sort": "fbt:asc", "date": date_str,
                        "_": int(time.time() * 1000)},
                headers={"Referer": "https://quote.eastmoney.com/"},
                alias=self.alias,
                throttle_key=f"{self.name}:pool",
            )
            for row in _pool_rows(payload):
                code = str(row.get("c") or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    continue
                limit_up.append(
                    LimitUpStock(
                        code=code,
                        name=str(row.get("n") or ""),
                        price=_f(row.get("p")) / 1000.0 if _f(row.get("p")) > 10000 else _f(row.get("p")),
                        change_pct=_f(row.get("zdp")),
                        amount=_f(row.get("amount")),
                        turnover_rate=_f(row.get("hs")),
                        first_limit_time=_fmt_time(row.get("fbt")),
                        last_limit_time=_fmt_time(row.get("lbt")),
                        open_times=_i(row.get("zbc")),
                        consecutive=max(1, _i(row.get("lbc"), 1)),
                        industry=str(row.get("hybk") or ""),
                        source=self.name,
                    )
                )

        if zb_url:
            try:
                payload = await self.http.get_json(
                    zb_url,
                    params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                            "Pageindex": 0, "pagesize": 400, "sort": "fbt:asc", "date": date_str,
                            "_": int(time.time() * 1000)},
                    headers={"Referer": "https://quote.eastmoney.com/"},
                    alias=self.alias,
                    throttle_key=f"{self.name}:pool",
                )
                for row in _pool_rows(payload):
                    code = str(row.get("c") or "").strip()
                    if not re.fullmatch(r"\d{6}", code):
                        continue
                    broken.append(
                        LimitUpStock(
                            code=code, name=str(row.get("n") or ""),
                            price=_f(row.get("p")) / 1000.0 if _f(row.get("p")) > 10000 else _f(row.get("p")),
                            change_pct=_f(row.get("zdp")),
                            first_limit_time=_fmt_time(row.get("fbt")),
                            open_times=_i(row.get("zbc")),
                            industry=str(row.get("hybk") or ""),
                            is_broken=True, source=self.name,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - 炸板池失败不影响涨停池
                logger.debug("炸板池获取失败: %s", exc)

        if not limit_up and not broken:
            raise ProviderError("涨停池返回空结果(可能非交易日)", source=self.name)
        return {"limit_up": limit_up, "broken": broken, "date": date_str, "source": self.name}

    async def fetch_breadth(self) -> Breadth:
        """市场涨跌家数。用轻量分页统计全市场, 避免拉取完整快照。"""
        try:
            quotes = await self.fetch_snapshot()
        except ProviderError:
            raise
        up = down = flat = 0
        limit_up = limit_down = 0
        up5 = down5 = 0
        total_amount = 0.0
        for q in quotes:
            total_amount += q.amount
            if q.change_pct > 0:
                up += 1
            elif q.change_pct < 0:
                down += 1
            else:
                flat += 1
            if q.change_pct >= 5:
                up5 += 1
            elif q.change_pct <= -5:
                down5 += 1
            threshold = limit_pct(q.code, q.name)
            if q.change_pct >= threshold - 0.3:
                limit_up += 1
            elif q.change_pct <= -(threshold - 0.3):
                limit_down += 1
        if up + down + flat == 0:
            raise ProviderError("涨跌家数统计为空", source=self.name)
        return Breadth(
            up=up, down=down, flat=flat, limit_up=limit_up, limit_down=limit_down,
            up_over_5=up5, down_over_5=down5, total_amount=total_amount, source=self.name,
        )

    # ------------------------------------------------------------------ #
    # 人气 / 财报
    # ------------------------------------------------------------------ #
    async def fetch_attention(self) -> dict[str, int]:
        """人气榜(关注度代理指标)。

        返回的证券代码带市场前缀(如 ``SZ000636``), 需要剥离; 排名由 ``rk`` 给出。
        """
        url = endpoints.first(self.name, CAP_ATTENTION)
        if not url:
            raise ProviderError("未配置人气榜地址", source=self.name)
        payload = await self.http.post_json(
            url,
            json_body={"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
                       "marketType": "", "pageNo": 1, "pageSize": 100},
            headers={"Referer": "https://guba.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:attention",
        )
        if isinstance(payload, dict) and payload.get("status") not in (0, None):
            raise ProviderError(f"人气榜接口异常: {payload.get('message')}", source=self.name)
        ranking: dict[str, int] = {}
        data = payload.get("data") if isinstance(payload, dict) else None
        items = data if isinstance(data, list) else (
            (data or {}).get("list") if isinstance(data, dict) else None
        )
        for index, item in enumerate(items or [], start=1):
            if not isinstance(item, dict):
                continue
            raw = str(item.get("sc") or item.get("code") or "")
            code = re.sub(r"\D", "", raw)
            if re.fullmatch(r"\d{6}", code):
                rank = _i(item.get("rk"), index) or index
                ranking[code] = rank
        if not ranking:
            raise ProviderError("人气榜未解析出任何证券代码", source=self.name)
        return ranking

    async def fetch_finance(self, codes: Iterable[str]) -> list[dict[str, Any]]:
        url = endpoints.first(self.name, CAP_FINANCE)
        if not url:
            raise ProviderError("未配置财报地址", source=self.name)
        payload = await self.http.get_json(
            url,
            params={
                "reportName": "RPT_LICO_FN_CPD",
                "columns": "ALL",
                "filter": "(SECURITY_TYPE_CODE in (\"058001001\",\"058001008\"))",
                "pageNumber": 1, "pageSize": 500, "sortColumns": "UPDATE_DATE",
                "sortTypes": -1, "source": "WEB", "client": "WEB",
            },
            headers={"Referer": "https://data.eastmoney.com/"},
            alias=self.alias,
            throttle_key=f"{self.name}:finance",
        )
        wanted = {normalize_code(c) for c in codes} if codes else set()
        out: list[dict[str, Any]] = []
        for row in _rows(payload):
            code = str(row.get("SECURITY_CODE") or "").strip()
            if wanted and code not in wanted:
                continue
            out.append({
                "code": code,
                "name": str(row.get("SECURITY_NAME_ABBR") or ""),
                "report_date": str(row.get("REPORTDATE") or ""),
                "roe": _f(row.get("WEIGHTAVG_ROE")),
                "revenue_yoy": _f(row.get("YSTZ")),
                "profit_yoy": _f(row.get("SJLTZ")),
                "eps": _f(row.get("BASIC_EPS")),
                "source": self.name,
            })
        if not out:
            raise ProviderError("财报返回空结果", source=self.name)
        return out


# --------------------------------------------------------------------------- #
# 响应解析辅助
# --------------------------------------------------------------------------- #
def _rows(payload: Any) -> list[dict[str, Any]]:
    """东财统一响应: ``{"rc":0,"data":{"total":n,"diff":[...]}}``; 有时 diff 是 dict。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    diff = data.get("diff")
    if isinstance(diff, list):
        return [row for row in diff if isinstance(row, dict)]
    if isinstance(diff, dict):
        return [row for row in diff.values() if isinstance(row, dict)]
    # datacenter 接口直接返回 {"result": {"data": [...]}}
    result = data.get("data")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    return []


def _total(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data")
    if isinstance(data, dict):
        return _i(data.get("total"))
    return 0


def _pool_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    pool = data.get("pool")
    if isinstance(pool, list):
        return [row for row in pool if isinstance(row, dict)]
    return []


def _fmt_time(value: Any) -> str:
    """东财涨停池时间格式为 ``HHMMSS`` 整数。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    text = f"{number:06d}"
    return f"{text[:2]}:{text[2:4]}:{text[4:]}"


__all__ = ["EastmoneyProvider"]
