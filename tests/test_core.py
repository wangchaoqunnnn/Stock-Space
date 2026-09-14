"""核心工具与规则的单元测试(不依赖网络)。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from stock_space.core.cache import BoundedTTLCache
from stock_space.core.util import (
    board_of,
    detect_market,
    from_symbol,
    limit_pct,
    market_session,
    normalize_code,
    secid,
    to_symbol,
)
from stock_space.engines import indicators as ind
from stock_space.engines.base import Condition, Signal, build_series
from stock_space.models import Bar, KLine, Quote
from stock_space.providers.base import CAP_KLINE, CAP_QUOTE, Provider
from stock_space.providers.synthetic import SyntheticProvider
from stock_space.providers.ths import parse_line_payload
from stock_space.services.push_service import clip_bytes, pusher


# --------------------------------------------------------------------------- #
# 代码与板块规则
# --------------------------------------------------------------------------- #
class TestCodeRules:
    @pytest.mark.parametrize("raw,expected", [
        ("600519", "600519"), ("sh600519", "600519"), ("SH600519", "600519"),
        ("600519.SH", "600519"), ("sz000001", "000001"), ("000001.SZ", "000001"),
        ("920001", "920001"), ("bj920001", "920001"), (" 300750 ", "300750"),
        ("688981", "688981"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_code(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", "60051", "6005199", "sh", "******"])
    def test_normalize_invalid(self, raw):
        with pytest.raises(ValueError):
            normalize_code(raw)

    @pytest.mark.parametrize("code,market", [
        ("600519", "sh"), ("601398", "sh"), ("688981", "sh"), ("900901", "sh"),
        ("000001", "sz"), ("002594", "sz"), ("300750", "sz"), ("301001", "sz"),
        ("920001", "bj"), ("830799", "bj"), ("430047", "bj"), ("871981", "bj"),
    ])
    def test_detect_market(self, code, market):
        assert detect_market(code) == market

    @pytest.mark.parametrize("code,board", [
        ("600519", "主板"), ("000001", "主板"), ("002594", "主板"),
        ("300750", "创业板"), ("301001", "创业板"),
        ("688981", "科创板"), ("689009", "科创板"),
        ("920001", "北交所"), ("830799", "北交所"),
    ])
    def test_board(self, code, board):
        assert board_of(code) == board

    @pytest.mark.parametrize("code,name,pct", [
        ("600519", "", 10.0),
        ("000001", "", 10.0),
        ("300750", "", 20.0),
        ("688981", "", 20.0),
        ("920001", "", 30.0),
        ("830799", "", 30.0),
        ("600519", "ST某某", 5.0),
        ("300750", "*ST某某", 5.0),
    ])
    def test_limit_pct(self, code, name, pct):
        assert limit_pct(code, name) == pct

    def test_symbol_roundtrip(self):
        assert to_symbol("600519") == "sh600519"
        assert to_symbol("000001") == "sz000001"
        assert to_symbol("920001") == "bj920001"
        assert to_symbol("600519", upper=True) == "SH600519"
        assert from_symbol("sh600519") == ("sh", "600519")
        assert from_symbol("600519") == ("sh", "600519")

    def test_secid(self):
        assert secid("600519") == "1.600519"
        assert secid("000001") == "0.000001"
        assert secid("920001") == "0.920001"


class TestMarketSession:
    def test_returns_known_phase(self):
        session = market_session()
        assert session.phase in (
            "pre_open", "call_auction", "trading", "lunch_break", "closed", "weekend", "holiday"
        )
        assert isinstance(session.interval_seconds, int)
        assert session.interval_seconds > 0

    def test_weekend_is_not_trading_day(self):
        from datetime import datetime
        from stock_space.core.util import CN_TZ

        # 2026-09-12 是周六
        saturday = datetime(2026, 9, 12, 10, 30, tzinfo=CN_TZ)
        session = market_session(saturday)
        assert session.is_trading_day is False
        assert session.should_poll is False

    def test_trading_hours(self):
        from datetime import datetime
        from stock_space.core.util import CN_TZ

        monday = datetime(2026, 9, 14, 10, 30, tzinfo=CN_TZ)
        session = market_session(monday)
        assert session.is_trading is True
        assert session.phase == "trading"
        assert session.should_poll is True

        lunch = market_session(datetime(2026, 9, 14, 12, 0, tzinfo=CN_TZ))
        assert lunch.phase == "lunch_break"
        assert lunch.is_trading is False


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
class TestCache:
    def test_capacity_and_lru(self):
        cache = BoundedTTLCache("t", max_entries=3, ttl_seconds=60)
        for i in range(5):
            cache.set(f"k{i}", i)
        assert cache.size == 3
        assert cache.get("k0") is None      # 最早写入的被淘汰
        assert cache.get("k4") == 4
        assert cache.stats().lru_evicted == 2

    def test_ttl_expiry(self):
        cache = BoundedTTLCache("t", max_entries=10, ttl_seconds=0.01)
        cache.set("a", 1)
        assert cache.get("a") == 1
        import time
        time.sleep(0.05)
        assert cache.get("a") is None
        assert cache.purge_expired() == 0   # 读取时已清理

    def test_get_or_set(self):
        cache = BoundedTTLCache("t", max_entries=10, ttl_seconds=60)
        calls = []

        def factory():
            calls.append(1)
            return "value"

        assert cache.get_or_set("k", factory) == "value"
        assert cache.get_or_set("k", factory) == "value"
        assert len(calls) == 1

    def test_trim_and_clear(self):
        cache = BoundedTTLCache("t", max_entries=100, ttl_seconds=60)
        for i in range(100):
            cache.set(str(i), i)
        removed = cache.trim_to(0.5)
        assert removed == 50
        assert cache.size == 50
        assert cache.clear() == 50

    def test_stats_hit_rate(self):
        cache = BoundedTTLCache("t", max_entries=10, ttl_seconds=60)
        cache.set("a", 1)
        cache.get("a")
        cache.get("missing")
        stats = cache.stats()
        assert stats.hits == 1 and stats.misses == 1
        assert stats.hit_rate == 0.5


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
class TestIndicators:
    def test_sma(self):
        result = ind.sma([1, 2, 3, 4, 5], 3)
        assert math.isnan(result[0]) and math.isnan(result[1])
        assert result[2] == pytest.approx(2.0)
        assert result[4] == pytest.approx(4.0)

    def test_sma_short_series(self):
        result = ind.sma([1, 2], 5)
        assert all(math.isnan(v) for v in result)

    def test_ema(self):
        result = ind.ema([1, 2, 3, 4, 5], 3)
        assert result[0] == 1
        assert result[-1] > result[0]

    def test_atr_positive(self):
        highs = [10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18, 17, 19, 20]
        lows = [9, 10, 11, 10, 12, 13, 12, 14, 15, 14, 16, 17, 16, 18, 19]
        closes = [9.5, 10.5, 11.5, 10.5, 12.5, 13.5, 12.5, 14.5, 15.5, 14.5, 16.5, 17.5, 16.5, 18.5, 19.5]
        result = ind.atr(highs, lows, closes, 14)
        assert ind.last_valid(result) > 0

    def test_rsi_bounds(self):
        closes = list(range(100, 140))
        result = ind.rsi(closes, 14)
        value = ind.last_valid(result, 50)
        assert 0 <= value <= 100
        assert value > 90   # 单调上涨 → 接近 100

    def test_macd_shape(self):
        closes = [10 + math.sin(i / 5) for i in range(80)]
        dif, dea, hist = ind.macd(closes)
        assert len(dif) == len(dea) == len(hist) == 80
        assert all(math.isfinite(v) for v in dif[30:])

    def test_kdj_bounds(self):
        highs = [10 + i * 0.1 for i in range(40)]
        lows = [9 + i * 0.1 for i in range(40)]
        closes = [9.5 + i * 0.1 for i in range(40)]
        k, d, j = ind.kdj(highs, lows, closes)
        assert all(math.isnan(v) for v in k[:8])
        assert all(math.isfinite(v) for v in k[10:])

    def test_boll_order(self):
        closes = [10 + math.sin(i / 3) for i in range(60)]
        upper, mid, lower = ind.boll(closes, 20)
        assert ind.last_valid(upper) > ind.last_valid(mid) > ind.last_valid(lower)

    def test_max_drawdown(self):
        assert ind.max_drawdown([10, 12, 6, 8]) == pytest.approx(50.0)
        assert ind.max_drawdown([1, 2, 3]) == 0.0

    def test_pct_change(self):
        assert ind.pct_change([10, 11], 1) == pytest.approx(10.0)
        assert ind.pct_change([10], 1) == 0.0

    def test_slope(self):
        assert ind.slope([1, 2, 3, 4, 5], 5) > 0
        assert ind.slope([5, 4, 3, 2, 1], 5) < 0

    def test_tag_indicators_json_friendly(self):
        bars = 80
        closes = [10 + math.sin(i / 4) * 2 for i in range(bars)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        volumes = [1e6 * (1 + (i % 5) * 0.1) for i in range(bars)]
        result = ind.tag_indicators(highs, lows, closes, volumes)
        assert set(result.keys()) >= {"ma20", "ma60", "dif", "dea", "k", "rsi14", "atr14"}
        for key, series in result.items():
            assert len(series) == bars, key
            for value in series:
                assert value is None or isinstance(value, float)


# --------------------------------------------------------------------------- #
# 数据源基类
# --------------------------------------------------------------------------- #
class TestProviderContract:
    def test_capabilities_declared(self):
        provider = SyntheticProvider()
        assert provider.supports(CAP_QUOTE)
        assert provider.supports(CAP_KLINE)
        assert not provider.supports("nonexistent")

    def test_health_hidden_from_capability_list(self):
        provider = SyntheticProvider()
        assert "health" not in provider.capabilities_ordered

    async def test_unsupported_method_raises(self):
        class Minimal(Provider):
            name = "minimal"
            label = "最小源"
            capabilities = frozenset({CAP_QUOTE})

        provider = Minimal()
        with pytest.raises(NotImplementedError):
            await provider.fetch_kline("600519")

    async def test_health_capability_always_supported(self):
        class Minimal(Provider):
            name = "minimal"
            label = "最小源"
            capabilities = frozenset({CAP_QUOTE})

        provider = Minimal()
        assert provider.supports("health") is True
        assert "health" not in provider.capabilities_ordered


class TestSinaMoneyFlow:
    """新浪个股资金流解析（来源：92KeBi `real/moneyflow.py`）。

    这是 ``money_flow`` 能力**唯一非东财的源**。此前该能力只有 eastmoney 一个候选，
    东财被阻断就没有任何兜底。这里锁定两件最容易写错的事：
    **单位换算**（新浪是元、东财是万元）与**分级字段的取舍**（不伪造缺失的桶）。
    """

    #: 真实响应片段（600519，2026-09-11 实测）。netamount=-270430816.75 元，
    #: ratioamount=-0.0620763，|net/ratio| 反推成交额 43.56 亿 —— 与"换手 27.37 万手
    #: × 1276.5 元 ≈ 34.9 亿"同量级，故 netamount 单位确为**元**。
    _PAYLOAD = [
        {"opendate": "2026-09-11", "trade": "1276.5000", "turnover": "27.3725",
         "netamount": "-270430816.7500", "ratioamount": "-0.0620763",
         "r0_net": "-396059819.0700", "r0_ratio": "-0.09091386"},
        {"opendate": "2026-09-10", "trade": "1284.7900", "turnover": "14.928",
         "netamount": "-111111111.0000", "ratioamount": "-0.02",
         "r0_net": "-22222222.0000", "r0_ratio": "-0.01"},
    ]

    def _provider(self, payload):
        from stock_space.providers.sina import SinaProvider

        provider = SinaProvider()

        async def fake_get_json(url, **kwargs):  # noqa: ANN001, ANN003, ANN202
            return payload

        provider.http = type("H", (), {"get_json": staticmethod(fake_get_json)})()
        return provider

    async def test_units_swapped_to_wan(self):
        """新浪是元，必须换算成与东财一致的万元（否则两源差 10000 倍）。"""
        flow = await self._provider(self._PAYLOAD).fetch_money_flow("600519")
        assert flow.main_net == pytest.approx(-270430816.75 / 1e4)
        assert flow.super_net == pytest.approx(-396059819.07 / 1e4)
        #: 大单 = 主力 - 超大单
        assert flow.large_net == pytest.approx((-270430816.75 + 396059819.07) / 1e4)

    async def test_ratio_converted_to_percent(self):
        """ratioamount 是小数比例，模型里的 main_net_pct 是百分数。"""
        flow = await self._provider(self._PAYLOAD).fetch_money_flow("600519")
        assert flow.main_net_pct == pytest.approx(-6.20763, abs=1e-4)

    async def test_missing_buckets_not_fabricated(self):
        """中/小单该接口确实没有 —— 必须留 0，不能编一个值出来。"""
        flow = await self._provider(self._PAYLOAD).fetch_money_flow("600519")
        assert flow.medium_net == 0.0
        assert flow.small_net == 0.0
        assert flow.source == "sina"

    async def test_takes_latest_row(self):
        """接口按最新在前返回，必须取第一条而不是最后一条。"""
        flow = await self._provider(self._PAYLOAD).fetch_money_flow("600519")
        assert flow.main_net == pytest.approx(-270430816.75 / 1e4)

    async def test_empty_and_malformed_payload_raise(self):
        from stock_space.core.http import ProviderError

        for payload in ([], "not-a-list", [{"netamount": "0", "ratioamount": "0"}]):
            with pytest.raises(ProviderError):
                await self._provider(payload).fetch_money_flow("600519")


class TestThsParser:
    def test_parse_line_payload(self):
        payload = (
            'quotebridge_v6_line_hs_600519_01_last({"data":"'
            '20260910,1600.00,1620.00,1590.00,1610.00,3000000,4800000000.00,1.50,,0,0;'
            '20260911,1610.00,1630.00,1600.00,1625.00,2500000,4000000000.00,1.20,,0,0",'
            '"num":2})'
        )
        bars = parse_line_payload(payload)
        assert len(bars) == 2
        assert bars[0].date == "2026-09-10"
        assert bars[0].open == 1600.0
        assert bars[0].close == 1610.0
        assert bars[0].high == 1620.0
        assert bars[0].low == 1590.0
        assert bars[0].volume == pytest.approx(30000.0)   # 股 → 手
        assert bars[1].change_pct != 0

    def test_parse_line_payload_invalid(self):
        from stock_space.core.http import ProviderError

        with pytest.raises(ProviderError):
            parse_line_payload("not json")


# --------------------------------------------------------------------------- #
# 序列构造
# --------------------------------------------------------------------------- #
class TestSeriesAndSignal:
    def _make(self, bars: int = 160) -> tuple[Quote, KLine]:
        rows = []
        price = 10.0
        for i in range(bars):
            price *= 1.002
            rows.append(Bar(date=f"2026-01-{i + 1:02d}", open=price * 0.99, high=price * 1.01,
                            low=price * 0.98, close=price, volume=1e6 + i * 1000,
                            amount=(1e6 + i * 1000) * price))
        quote = Quote(code="600519", name="测试", price=price, prev_close=price * 0.998,
                      change_pct=0.2, volume=1e6, amount=1e6 * price, turnover_rate=2.0)
        return quote, KLine(code="600519", name="测试", bars=rows)

    def test_build_series(self):
        quote, kline = self._make()
        series = build_series(quote, kline)
        assert series.length == 160
        assert series.close > 0
        assert series.ma(20) > 0
        assert series.ma(60) > 0
        assert series.board == "主板"
        assert series.limit_pct == 10.0
        assert 0 <= series.position_in_range(120) <= 1

    def test_score_from_weights(self):
        conditions = [
            Condition("a", True, weight=0.5),
            Condition("b", False, weight=0.5),
        ]
        assert Signal.__module__  # 触发导入
        from stock_space.engines.base import Strategy

        assert Strategy.score_from(conditions) == pytest.approx(50.0)
        assert Strategy.score_from([Condition("a", True)]) == pytest.approx(100.0)
        assert Strategy.score_from([]) == 0.0

    def test_signal_as_dict_json_safe(self):
        quote, kline = self._make()
        series = build_series(quote, kline)
        signal = Signal(strategy="x", code=series.code, name=series.name, score=88.888,
                        reasons=[Condition("条件", True, value=np.float64(1.5), weight=1.0)],
                        metrics={"nan": float("nan"), "arr": np.array([1, 2])})
        payload = signal.as_dict()
        assert payload["score"] == 88.89
        assert payload["reasons"][0]["value"] == 1.5
        assert payload["metrics"]["nan"] is None
        assert payload["metrics"]["arr"] == [1, 2]


# --------------------------------------------------------------------------- #
# 推送工具
# --------------------------------------------------------------------------- #
class TestPushHelpers:
    def test_clip_bytes_multibyte_safe(self):
        text = "中文测试" * 1000
        clipped = clip_bytes(text, 100)
        assert len(clipped.encode("utf-8")) <= 100
        assert clipped.endswith("...")
        assert "中" in clipped

    def test_clip_bytes_noop_when_short(self):
        assert clip_bytes("abc", 100) == "abc"

    def test_fingerprint_stable(self):
        a = pusher.fingerprint_of("标题", "正文", "kind")
        b = pusher.fingerprint_of("标题", "正文", "kind")
        c = pusher.fingerprint_of("标题2", "正文", "kind")
        assert a == b and a != c

    def test_message_markdown_contains_link_and_disclaimer(self):
        from stock_space.services.push_service import PushMessage

        message = PushMessage(title="标题", body="正文", url="http://example.invalid/page")
        markdown = message.to_markdown()
        assert "## 标题" in markdown
        assert "http://example.invalid/page" in markdown
        assert "不构成投资建议" in markdown


# --------------------------------------------------------------------------- #
# JSON 包装解析(踩过的真实坑)
# --------------------------------------------------------------------------- #
class TestJsonPayloadParsing:
    """上游返回的 JSON 有纯 JSON、函数式 JSONP、变量式 JSONP 三种包装。

    变量式(``kline_day={...}``)曾经让"腾讯 K 线入口看起来全部失败":
    HTTP 200 但按函数式 JSONP 解析会报错, 于是误判为源不可用并整体换源。
    """

    def test_plain_json(self):
        from stock_space.core.http import parse_json_payload

        assert parse_json_payload('{"a": 1}') == {"a": 1}
        assert parse_json_payload("[1, 2, 3]") == [1, 2, 3]

    def test_function_jsonp(self):
        from stock_space.core.http import parse_json_payload

        assert parse_json_payload('cb({"a": 1});') == {"a": 1}
        assert parse_json_payload('quotebridge_x({"data": "1;2"})') == {"data": "1;2"}

    def test_variable_jsonp(self):
        from stock_space.core.http import parse_json_payload

        payload = 'kline_day={"code":0,"data":{"sh600519":{"qfqday":[["2026-09-11","1","2","3","4","5"]]}}}'
        parsed = parse_json_payload(payload)
        assert parsed["code"] == 0
        assert parsed["data"]["sh600519"]["qfqday"][0][0] == "2026-09-11"

    def test_bom_prefixed(self):
        from stock_space.core.http import parse_json_payload

        assert parse_json_payload('\ufeff{"a": 1}') == {"a": 1}

    def test_invalid_raises_provider_error(self):
        from stock_space.core.http import ProviderError, parse_json_payload

        with pytest.raises(ProviderError):
            parse_json_payload("<!DOCTYPE html><html>WAF</html>")
        with pytest.raises(ProviderError):
            parse_json_payload("")


# --------------------------------------------------------------------------- #
# 可等待对象归一化(另一个踩过的真实坑)
# --------------------------------------------------------------------------- #
class TestEnsureAwaitable:
    """``await bound_method`` 不会执行方法, 而是立刻返回方法对象本身且不报错。

    这会让"可选数据块"永远拿到一个函数, 被 except 静默吞掉, 表现为
    "某个面板长期为空但日志里什么都没有"。``_ensure_awaitable`` 显式修掉这一点。
    """

    async def test_coroutine_passthrough(self):
        from stock_space.services.market_service import _ensure_awaitable

        async def factory() -> str:
            return "value"

        assert await _ensure_awaitable(factory()) == "value"

    async def test_callable_is_invoked(self):
        from stock_space.services.market_service import _ensure_awaitable

        class Holder:
            async def method(self) -> str:
                return "from-method"

        holder = Holder()
        # 传入的是"方法对象"而不是调用结果
        assert await _ensure_awaitable(holder.method) == "from-method"

    async def test_plain_value_wrapped(self):
        from stock_space.services.market_service import _ensure_awaitable

        assert await _ensure_awaitable(42) == 42

    async def test_safe_returns_none_on_failure(self):
        from stock_space.services.market_service import _safe

        async def boom() -> None:
            raise RuntimeError("expected")

        assert await _safe(boom()) is None
        assert await _safe(boom) is None


# --------------------------------------------------------------------------- #
# 复盘报告的合规校验
# --------------------------------------------------------------------------- #
class TestReviewCompliance:
    """合规校验必须"只针对具体标的"判定, 否则会把平台自己的说明文案误判为荐股。

    实测踩到过: 报告里的策略说明与风险提示合法地包含"买入/卖出"等词,
    如果按全文包含判定, 每一份真实报告都会被判为不合规。
    """

    def _report(self, **overrides) -> dict:
        base = {
            "kind": "cn_close",
            "title": "A股收盘复盘",
            "conclusion": "赚钱效应 温和。",
            "risk_notice": "本文仅为行情复盘参考，不构成任何投资建议。",
            "modules": [
                {"key": "indices", "title": "大盘指数概览", "status": "ok",
                 "bullets": ["上证指数 3894.28（+0.16%）"]},
            ],
        }
        base.update(overrides)
        return base

    def test_clean_report_passes(self):
        from stock_space.engines.review import check_compliance

        result = check_compliance(self._report())
        assert result["ok"] is True, result["issues"]

    def test_strategy_wording_is_exempt(self):
        """平台自身的策略说明里出现"买卖点/破位坚决跑"不应被判违规。"""
        from stock_space.engines.review import check_compliance

        report = self._report(conclusion="策略规则：买卖看支撑，破位坚决跑；不构成投资建议。")
        report["modules"].append({
            "key": "desc", "title": "策略说明", "status": "ok",
            "bullets": ["形态看均线，买卖看支撑", "不推荐股票，仅为研究"],
        })
        assert check_compliance(report)["ok"] is True

    def test_advice_on_specific_stock_is_flagged(self):
        from stock_space.engines.review import check_compliance

        report = self._report(conclusion="建议买入 600519，目标价 1500。")
        result = check_compliance(report)
        assert result["ok"] is False
        assert any("荐股" in issue for issue in result["issues"])

    def test_missing_risk_notice_is_flagged(self):
        from stock_space.engines.review import check_compliance

        report = self._report(risk_notice="")
        result = check_compliance(report)
        assert result["ok"] is False
        assert any("风险提示" in issue for issue in result["issues"])

    def test_build_report_passes_compliance(self):
        from stock_space.engines.review import ReviewInput, build_report

        report = build_report(ReviewInput(source="synthetic"))
        assert report["compliance"]["ok"] is True, report["compliance"]["issues"]
        assert "不构成" in report["risk_notice"]
