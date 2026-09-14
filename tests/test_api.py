"""API 集成测试 —— 通过 ASGI 直连应用, 覆盖主要接口契约。

不启动真实服务器、不访问外网(synthetic 模式), 因此可以在 CI 里稳定运行。
"""

from __future__ import annotations

import re

import pytest

from conftest import unwrap

from stock_space.engines import STRATEGY_ORDER


# --------------------------------------------------------------------------- #
# 系统与健康
# --------------------------------------------------------------------------- #
class TestSystemAPI:
    async def test_health(self, client):
        data = unwrap(await client.get("/api/health"))
        assert data["status"] in ("ok", "degraded")
        assert data["version"]
        assert data["rss_mb"] > 0
        assert data["capabilities_total"] > 0
        assert isinstance(data["degraded"], list)

    async def test_ready(self, client):
        data = unwrap(await client.get("/api/ready"))
        assert data["ready"] is True
        assert data["issues"] == []

    async def test_version(self, client):
        data = unwrap(await client.get("/api/version"))
        assert data["version"]
        assert data["title"]

    async def test_healthz_probe(self, client):
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_overview(self, client):
        data = unwrap(await client.get("/api/system/overview"))
        assert data["app"]["version"]
        assert "memory" in data and "scheduler" in data and "database" in data
        assert data["sources"]["providers"] >= 5
        assert "software" not in data

    async def test_memory_report(self, client):
        data = unwrap(await client.get("/api/system/memory", params={"points": 30}))
        assert data["process"]["rss_mb"] > 0
        assert data["limits"]["soft_limit_mb"] > 0
        assert data["limits"]["hard_limit_mb"] > data["limits"]["soft_limit_mb"]
        assert len(data["caches"]["items"]) >= 4
        names = {item["name"] for item in data["caches"]["items"]}
        assert {"kline_memory", "provider_snapshot"} & names

    async def test_memory_flush(self, client, warm_market):
        data = unwrap(await client.post("/api/system/memory/flush"))
        assert "freed_entries" in data
        assert "rss_after_mb" in data

    async def test_memory_raw(self, client):
        data = unwrap(await client.get("/api/system/memory/raw"))
        assert data["rss_mb"] > 0
        assert "cache_entries" in data

    async def test_scheduler_report(self, client):
        data = unwrap(await client.get("/api/system/scheduler"))
        assert "running" in data
        assert "jobs" in data
        assert "session" in data

    async def test_jobs_list(self, client):
        data = unwrap(await client.get("/api/system/jobs", params={"limit": 10}))
        assert isinstance(data["items"], list)

    async def test_database_stats(self, client):
        data = unwrap(await client.get("/api/system/database"))
        assert "rows" in data
        assert "kline_daily" in data["rows"]

    async def test_database_cleanup(self, client):
        data = unwrap(await client.post("/api/system/database/cleanup"))
        assert isinstance(data, dict)

    async def test_integration_map(self, client):
        data = unwrap(await client.get("/api/system/integration"))
        projects = {item["project"] for item in data["items"]}
        for expected in ("92KeBi", "NPatternStrategy", "TrendSniper", "QuietRiseScanner"):
            assert expected in projects
        assert len(projects) >= 11


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
class TestMarketAPI:
    async def test_clock(self, client):
        data = unwrap(await client.get("/api/market/clock"))
        assert data["phase"] in (
            "pre_open", "call_auction", "trading", "lunch_break", "closed", "weekend", "holiday"
        )
        assert data["timezone"] == "UTC+8"
        assert data["refresh_hint_seconds"] > 0

    async def test_dashboard(self, client, warm_market):
        data = unwrap(await client.get("/api/dashboard"))
        assert data["universe_size"] > 0
        assert isinstance(data["degraded"], list)
        assert data["breadth"] is not None
        # 榜单至少有内容; 合成剧本下涨/跌榜可能有一侧为空, 因此至少要求"金额榜"非空
        assert data["leaders"]["amount"], "榜单数据为空"
        assert set(data["latest_scans"].keys()) >= set(STRATEGY_ORDER)
        # 快照必须带名称与代码(否则板块共振、龙头识别等一整条链路都会失效)
        sample = (data["leaders"]["amount"] or data["leaders"]["gainers"]
                  or data["leaders"]["losers"])[0]
        assert sample["name"], "快照缺少股票名称"
        assert sample["code"]

    async def test_snapshot_fields(self, client, warm_market):
        """全市场快照的字段完整性 —— 这是所有策略的输入底座。"""
        quotes = warm_market
        named = sum(1 for q in quotes if q.name)
        assert named == len(quotes), f"{len(quotes) - named} 只标的缺少名称"
        assert all(q.price > 0 for q in quotes), "存在价格为 0 的标的"
        assert any(q.industry for q in quotes), "快照完全没有行业字段"
        # 四个板块都要出现, 否则涨跌停幅度会算错
        boards = {q.board for q in quotes}
        for expected in ("主板", "创业板", "科创板", "北交所"):
            assert expected in boards, f"快照缺少 {expected} 标的"
        # 成交量统一口径为"股"
        assert all(q.volume == 0 or q.volume > 1000 for q in quotes[:200]), "成交量单位可能不是股"

    async def test_indices(self, client, warm_market):
        data = unwrap(await client.get("/api/market/indices"))
        assert data["total"] >= 6
        assert data["matched"] >= 0
        # 指数代码与个股代码段重叠(000001 既是上证指数也是平安银行),
        # 因此必须逐个核对"返回的名称确实是该指数", 否则就是拿个股冒充指数。
        by_code = {item["code"]: item for item in data["items"]}
        if "000001" in by_code and not by_code["000001"].get("missing"):
            assert "指数" in by_code["000001"]["name"] or "上证" in by_code["000001"]["name"], \
                f"000001 应当是上证指数, 实际返回 {by_code['000001']['name']}"
            assert by_code["000001"]["price"] > 100, "上证指数点位不应是个股价格量级"
        if "399001" in by_code and not by_code["399001"].get("missing"):
            assert "成指" in by_code["399001"]["name"] or "深证" in by_code["399001"]["name"], \
                f"399001 应当是深证成指, 实际返回 {by_code['399001']['name']}"
        for item in data["items"]:
            assert item["market"] in ("sh", "sz", "bj")

    async def test_breadth(self, client, warm_market):
        data = unwrap(await client.get("/api/market/breadth"))
        assert data["up"] + data["down"] + data["flat"] > 0
        assert 0 <= data["up_ratio"] <= 1

    @pytest.mark.parametrize("kind", ["gainers", "losers", "amount", "turnover"])
    async def test_rank(self, client, warm_market, kind):
        data = unwrap(await client.get("/api/market/rank", params={"kind": kind, "limit": 10}))
        assert data["kind"] == kind
        assert len(data["items"]) >= 1
        item = data["items"][0]
        for field in ("code", "name", "price", "change_pct", "amount"):
            assert field in item

    async def test_rank_invalid_kind(self, client):
        response = await client.get("/api/market/rank", params={"kind": "bogus"})
        assert response.status_code == 422

    async def test_sectors(self, client, warm_market):
        data = unwrap(await client.get("/api/market/sectors", params={"kind": "industry"}))
        assert len(data["items"]) > 0
        first = data["items"][0]
        assert "change_pct" in first
        # ⚠️ 回归：东财板块接口的名称在 f14，而 f13 是市场号（板块固定为 90）。
        # 曾经误用 f13 作名称，导致所有板块名都变成 "90"，
        # 前端「板块强弱」面板因此显示不出任何可读内容。
        for item in data["items"][:20]:
            assert item["name"], f"板块缺少名称: {item}"
            assert not str(item["name"]).strip().isdigit(), \
                f"板块名是纯数字（字段用错）: {item['name']!r}"
            assert item["code"], f"板块缺少代码: {item}"
        names = {item["name"] for item in data["items"][:20]}
        assert len(names) > 5, f"板块名称重复异常: {names}"

    async def test_sectors_concept(self, client, warm_market):
        data = unwrap(await client.get("/api/market/sectors", params={"kind": "concept"}))
        assert len(data["items"]) > 0
        for item in data["items"][:10]:
            assert item["name"] and not str(item["name"]).strip().isdigit()

    async def test_sector_fields_for_frontend(self, client, warm_market):
        """前端「板块强弱」面板依赖的字段必须齐全。"""
        data = unwrap(await client.get("/api/market/sectors", params={"kind": "industry"}))
        item = data["items"][0]
        for field in ("name", "code", "change_pct", "amount", "up_count", "down_count"):
            assert field in item, f"板块缺少前端依赖字段 {field}"
        assert isinstance(item["up_count"], int)
        assert isinstance(item["down_count"], int)

    async def test_sector_detail(self, client, warm_market):
        sectors = unwrap(await client.get("/api/market/sectors"))["items"]
        target = sectors[0]
        data = unwrap(await client.get("/api/market/sector/" + target["code"],
                                       params={"name": target["name"]}))
        assert data["name"] == target["name"]
        assert data["member_count"] >= 1
        assert len(data["members"]) == data["member_count"]

    async def test_limit_up_pool(self, client, warm_market):
        response = await client.get("/api/market/limit-up")
        if response.status_code == 200:
            data = unwrap(response)
            assert "limit_up_count" in data
            assert "ladder" in data
        else:
            # 合成剧本可能恰好没有涨停样本, 此时必须明确报错而不是返回空结构
            payload = response.json()
            assert payload["code"] != 0

    async def test_emotion(self, client, warm_market):
        data = unwrap(await client.get("/api/market/emotion"))
        assert 0 <= data["emotion"]["score"] <= 100
        assert len(data["emotion"]["parts"]) == 6
        assert data["cycle"]["phase"] in ("ice", "start", "ferment", "climax", "ebb")
        assert data["cycle"]["label"]
        assert 0 <= data["cycle"]["confidence"] <= 1
        assert data["market"]["sample_size"] > 0

    async def test_market_context(self, client, warm_market):
        data = unwrap(await client.get("/api/market/context"))
        assert data["env_gate"] in ("full", "half", "off")
        assert data["universe_size"] > 0
        assert "top_sectors" in data

    async def test_quotes(self, client, warm_market):
        codes = ",".join(q.code for q in warm_market[:5])
        data = unwrap(await client.get("/api/quotes", params={"codes": codes}))
        assert len(data["items"]) >= 1
        assert data["items"][0]["price"] > 0

    async def test_search(self, client, warm_market):
        keyword = warm_market[0].name[:2] or warm_market[0].code[:2]
        data = unwrap(await client.get("/api/search", params={"keyword": keyword, "limit": 5}))
        assert isinstance(data["items"], list)
        assert data["total_scanned"] > 0

    async def test_search_by_code(self, client, warm_market):
        code = warm_market[0].code
        data = unwrap(await client.get("/api/search", params={"keyword": code}))
        assert any(item["code"] == code for item in data["items"])

    async def test_stock_detail(self, client, warm_market):
        code = warm_market[0].code
        data = unwrap(await client.get(f"/api/stock/{code}"))
        assert data["code"] == code
        assert data["quote"] is not None
        assert data["kline"] is not None

    async def test_kline_with_indicators(self, client, warm_market):
        code = warm_market[0].code
        data = unwrap(await client.get(f"/api/stock/{code}/kline",
                                       params={"days": 120, "period": "day"}))
        assert data["count"] >= 60
        assert len(data["bars"]) == data["count"]
        assert "ma20" in data["indicators"]
        assert data["origin"] in ("disk", "network")

    async def test_kline_weekly(self, client, warm_market):
        code = warm_market[0].code
        data = unwrap(await client.get(f"/api/stock/{code}/kline", params={"period": "week"}))
        assert data["period"] == "week"

    async def test_money_flow_has_fallback_candidate(self, client, warm_market):
        """money_flow 必须不止一个候选源。

        真实缺口：该能力原本只有 ``eastmoney`` 一个候选，东财一旦被阻断就完全没有
        兜底，与"多源容灾"的要求不符。已补入新浪（来源：92KeBi real/moneyflow.py）。

        注意断言的是**配置顺序**而不是 ``registry.candidates()``：测试跑在
        synthetic 模式下，``candidates()`` 会刻意只返回 ``["synthetic"]``
        （避免"真实源失败→落到合成数据"的混合结果），看不到真实候选。
        """
        from stock_space.config import config

        order = config().capability_order("money_flow")
        assert len(order) >= 2, f"money_flow 仍是单点：{order}"
        assert "eastmoney" in order and "sina" in order, f"候选源不完整：{order}"

    async def test_money_flow_failover_to_sina(self, client, warm_market):
        """首选源失败时必须自动切到兜底源，并把切换过程记录下来。

        在真实模式下验证（synthetic 模式屏蔽了所有真实源）。
        """
        from stock_space.config import config
        from stock_space.core.http import ProviderError
        from stock_space.models import MoneyFlow
        from stock_space.providers.registry import registry

        async def em_fail(code):  # noqa: ANN001, ANN202
            raise ProviderError("模拟东财不可用", source="eastmoney")

        async def sina_ok(code):  # noqa: ANN001, ANN202
            return MoneyFlow(code=code, name="", main_net=-27043.08,
                             main_net_pct=-6.207, super_net=-39605.98,
                             large_net=12562.90, source="sina")

        try:
            config().set("data_sources.mode", "real")
            #: ⚠️ 两处顺序都很关键：
            #:  1) 必须**先切模式再 build**，否则候选里看不到真实源；
            #:  2) 必须**先 build 再取 provider 并打桩** —— build(force=True) 会重建
            #:     provider 实例，打桩在重建之前会被冲掉。
            #:     （registry.call 内部那次 build() 因 _built=True 会直接返回，不会重建。）
            registry.build(force=True)
            order = registry.candidates("money_flow")
            assert order[:2] == ["eastmoney", "sina"], f"真实模式候选顺序异常：{order}"

            em = next(p for p in registry.all() if p.name == "eastmoney")
            sina = next(p for p in registry.all() if p.name == "sina")
            original_em, original_sina = em.fetch_money_flow, sina.fetch_money_flow
            em.fetch_money_flow = em_fail
            sina.fetch_money_flow = sina_ok
            try:
                outcome = await registry.call("money_flow", warm_market[0].code)
                assert outcome.alias == "sina", f"未切到兜底源，实际用了 {outcome.alias}"
                assert outcome.value.source == "sina"
                assert outcome.value.main_net == -27043.08
                tried = [a.get("source") for a in outcome.attempts]
                assert "eastmoney" in tried and "sina" in tried, tried
            finally:
                em.fetch_money_flow = original_em
                sina.fetch_money_flow = original_sina
        finally:
            config().set("data_sources.mode", "synthetic")
            registry.build(force=True)

    async def test_kline_invalid_code(self, client):
        response = await client.get("/api/stock/abc/kline")
        assert response.status_code in (400, 422, 500)

    async def test_news(self, client):
        response = await client.get("/api/news")
        data = unwrap(response)
        assert "items" in data
        assert isinstance(data["items"], list)

    async def test_review(self, client, warm_market):
        data = unwrap(await client.get("/api/review", params={"kind": "cn_close"}))
        assert data["compliance"]["ok"] is True
        assert len(data["modules"]) >= 4
        assert "不构成" in data["risk_notice"]
        assert data["conclusion"]

    async def test_review_invalid_kind(self, client):
        response = await client.get("/api/review", params={"kind": "bogus"})
        assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 策略与回测
# --------------------------------------------------------------------------- #
class TestStrategyAPI:
    async def test_catalog(self, client):
        data = unwrap(await client.get("/api/strategies"))
        assert len(data["items"]) >= 5
        keys = [item["key"] for item in data["items"]]
        for expected in STRATEGY_ORDER:
            assert expected in keys
        for item in data["items"]:
            assert item["params"]
            assert item["param_defaults"]
            assert item["backtest"]
            assert item["rules"]

    async def test_strategy_detail(self, client):
        data = unwrap(await client.get("/api/strategies/trend"))
        assert data["key"] == "trend"
        assert data["logic"]
        assert data["params"]

    async def test_strategy_unknown(self, client):
        response = await client.get("/api/strategies/nope")
        assert response.status_code == 404

    async def test_pattern_catalog_included(self, client):
        data = unwrap(await client.get("/api/strategies/pattern"))
        assert len(data.get("patterns", [])) >= 16

    async def test_strategy_params_save_and_reset(self, client):
        saved = unwrap(await client.post("/api/strategies/trend/params",
                                         json={"min_score": 70, "bogus_key": 1}))
        assert saved["params"]["min_score"] == 70.0
        assert "bogus_key" in saved["rejected"]
        reset = unwrap(await client.delete("/api/strategies/trend/params"))
        assert reset["params"]["min_score"] == 60.0

    @pytest.mark.parametrize("key", STRATEGY_ORDER)
    async def test_scan_each_strategy(self, client, warm_market, key):
        data = unwrap(await client.post(f"/api/strategies/{key}/scan",
                                        params={"limit": 10, "persist": "false"}))
        assert data["strategy"] == key
        assert data["total_evaluated"] > 0
        assert isinstance(data["items"], list)
        assert "fetcher" in data
        if data["items"]:
            item = data["items"][0]
            assert 0 <= item["score"] <= 100
            assert item["reasons"]
            assert item["stop_loss"] >= 0

    async def test_scan_all(self, client, warm_market):
        data = unwrap(await client.post("/api/scan/all",
                                        params={"per_strategy": 5, "persist": "false"}))
        assert set(data["results"].keys()) == set(STRATEGY_ORDER)
        for key, outcome in data["results"].items():
            assert outcome["strategy"] == key

    async def test_last_scan_after_persist(self, client, warm_market):
        await client.post("/api/strategies/trend/scan", params={"limit": 5, "persist": "true"})
        data = unwrap(await client.get("/api/strategies/trend/last"))
        assert data["strategy"] == "trend"
        assert "items" in data

    # ------------------------------------------------------------ 异步扫描任务
    #: 背景：同步扫描实测单个策略要 158 秒，会被代理/浏览器空闲超时掐断，
    #: 用户看到的是"无法连接到后端服务"。异步任务把长耗时挪出 HTTP 请求。

    async def _wait_job(self, client, job_id, limit=400):
        """轮询到终态，返回任务对象（含 result）。"""
        for _ in range(limit):
            data = unwrap(await client.get(f"/api/scan/jobs/{job_id}"))
            if data.get("terminal"):
                return data
        raise AssertionError(f"任务 {job_id} 未在预期轮询次数内结束")

    async def test_scan_job_returns_immediately(self, client, warm_market):
        """创建任务必须立刻返回 job_id —— 这正是修复同步长请求的关键。"""
        data = unwrap(await client.post("/api/scan/jobs",
                                        params={"strategy": "trend", "limit": 5,
                                                "persist": "false"}))
        job = data["job"]
        assert job["id"], "必须返回任务 id 供前端轮询"
        assert job["kind"] == "trend"
        assert job["status"] in ("queued", "running", "succeeded")
        assert data["poll"].endswith(job["id"])

    async def test_scan_job_reports_progress_then_result(self, client, warm_market):
        data = unwrap(await client.post("/api/scan/jobs",
                                        params={"strategy": "trend", "limit": 5,
                                                "persist": "false"}))
        job = await self._wait_job(client, data["job"]["id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["percent"] == 100.0
        assert job["terminal"] is True
        result = job["result"]
        assert result["strategy"] == "trend"
        assert result["total_evaluated"] > 0
        assert isinstance(result["items"], list)

    async def test_scan_job_all_covers_every_strategy(self, client, warm_market):
        data = unwrap(await client.post("/api/scan/jobs",
                                        params={"strategy": "all", "per_strategy": 3,
                                                "persist": "false"}))
        job = await self._wait_job(client, data["job"]["id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["total"] == len(STRATEGY_ORDER)
        assert job["done"] == len(STRATEGY_ORDER)
        assert set(job["result"]["results"].keys()) == set(STRATEGY_ORDER)

    async def test_scan_job_unknown_strategy_and_job(self, client):
        response = await client.post("/api/scan/jobs", params={"strategy": "nope"})
        assert response.status_code == 404
        response = await client.get("/api/scan/jobs/deadbeefdeadbeef")
        assert response.status_code == 404

    async def test_scan_job_listing_hides_result_body(self, client, warm_market):
        """列表接口不能带完整结果体，否则一次列表就是几百 KB。"""
        await client.post("/api/scan/jobs",
                          params={"strategy": "trend", "limit": 3, "persist": "false"})
        data = unwrap(await client.get("/api/scan/jobs", params={"limit": 5}))
        assert data["items"], "应至少有一个任务"
        for item in data["items"]:
            assert "result" not in item
            assert {"id", "status", "percent", "kind"} <= set(item.keys())

    async def test_scan_progress_percent_monotonic(self, client, warm_market):
        """percent 必须落在 0~100 且随 done 单调不减（进度条依赖它）。"""
        data = unwrap(await client.post("/api/scan/jobs",
                                        params={"strategy": "all", "per_strategy": 2,
                                                "persist": "false"}))
        job_id = data["job"]["id"]
        last = -1.0
        for _ in range(400):
            job = unwrap(await client.get(f"/api/scan/jobs/{job_id}",
                                          params={"result": "false"}))
            assert 0.0 <= job["percent"] <= 100.0
            assert job["percent"] >= last, "percent 不应回退"
            assert job["done"] <= job["total"]
            last = job["percent"]
            if job.get("terminal"):
                break
        else:
            raise AssertionError("任务未结束")
        assert last == 100.0

    async def test_scan_reports_fine_grained_progress(self, client, warm_market):
        """单策略扫描必须上报"已评估 N/M"，否则进度条会长时间停在 0%。

        真实事故形态：一次全市场扫描要 2~3 分钟，若进度只在"策略完成"时跳一次，
        用户看到 0% 不动，无法区分"在跑"与"卡死"。
        """
        data = unwrap(await client.post("/api/scan/jobs",
                                        params={"strategy": "trend", "limit": 3,
                                                "persist": "false"}))
        job_id = data["job"]["id"]
        details = []
        for _ in range(400):
            job = unwrap(await client.get(f"/api/scan/jobs/{job_id}",
                                          params={"result": "false"}))
            if job.get("detail"):
                details.append(job["detail"])
            if job.get("terminal"):
                break
        assert details, "扫描过程中应上报 detail（已评估 N/M 只）"
        assert any("已评估" in d for d in details), details[:3]
        #: 终态必须清空 detail，避免前端把残留进度当成"还在跑"
        final = unwrap(await client.get(f"/api/scan/jobs/{job_id}",
                                        params={"result": "false"}))
        assert final["detail"] == ""
        assert final["percent"] == 100.0

    async def test_evaluate_single_stock(self, client, warm_market):
        code = warm_market[0].code
        data = unwrap(await client.post("/api/strategies/quiet_rise/evaluate", json={"code": code}))
        assert data["code"] == code
        assert 0 <= data["score"] <= 100
        assert data["reasons"]
        assert "context" in data

    async def test_evaluate_unknown_code(self, client):
        response = await client.post("/api/strategies/trend/evaluate", json={"code": "abc"})
        assert response.status_code in (400, 404, 422, 500)

    async def test_backtest(self, client, warm_market):
        codes = [q.code for q in warm_market[:8]]
        data = unwrap(await client.post("/api/backtest", json={
            "strategy": "trend", "codes": codes, "days": 250,
            "fill": "next_open", "max_positions": 3,
        }, timeout=600000))
        assert data["strategy"] == "trend"
        assert data["fill_mode"] == "next_open"
        assert "win_rate" in data["metrics"]
        assert data["metrics"]["closed_count"] >= 0
        assert data["cost_model"]["slippage_rate"] > 0
        assert len(data["equity_curve"]) > 0
        assert data["available"] >= 1

    async def test_backtest_unknown_strategy(self, client):
        response = await client.post("/api/backtest", json={"strategy": "nope"})
        assert response.status_code == 404

    async def test_signals_list(self, client):
        data = unwrap(await client.get("/api/signals", params={"limit": 10}))
        assert isinstance(data["items"], list)


# --------------------------------------------------------------------------- #
# 数据源
# --------------------------------------------------------------------------- #
class TestDatasourceAPI:
    async def test_list(self, client):
        data = unwrap(await client.get("/api/datasources"))
        assert data["mode"] == "synthetic"
        assert len(data["providers"]) >= 5
        assert len(data["categories"]) >= 15
        assert "capability_labels" in data
        for provider in data["providers"]:
            assert provider["name"]
            assert "metrics" in provider

    async def test_candidates(self, client):
        data = unwrap(await client.get("/api/datasources/kline/candidates"))
        assert data["capability"] == "kline"
        assert data["effective_order"] == ["synthetic"]

    async def test_candidates_unknown(self, client):
        response = await client.get("/api/datasources/bogus/candidates")
        assert response.status_code == 404

    async def test_lock_and_unlock(self, client):
        locked = unwrap(await client.post("/api/datasources/kline/lock", json={"alias": "synthetic"}))
        assert locked["locked"] == "synthetic"
        candidates = unwrap(await client.get("/api/datasources/kline/candidates"))
        assert candidates["locked"] == "synthetic"
        unlocked = unwrap(await client.post("/api/datasources/kline/lock", json={"alias": ""}))
        assert unlocked["locked"] is None
        await client.post("/api/datasources/unlock-all")

    async def test_lock_unknown_alias(self, client):
        response = await client.post("/api/datasources/kline/lock", json={"alias": "nonexistent"})
        assert response.status_code == 400

    async def test_lock_unknown_capability(self, client):
        response = await client.post("/api/datasources/bogus/lock", json={"alias": "synthetic"})
        assert response.status_code == 400

    async def test_toggle_source(self, client):
        data = unwrap(await client.post("/api/datasources/tencent/toggle", json={"enabled": False}))
        assert data["enabled"] is False
        assert "tencent" in data["disabled"]
        unwrap(await client.post("/api/datasources/tencent/toggle", json={"enabled": True}))
        data = unwrap(await client.get("/api/datasources"))
        assert "tencent" not in data["disabled"]

    async def test_probe_one(self, client):
        data = unwrap(await client.post("/api/datasources/synthetic/probe", timeout=60000))
        assert data["name"] == "synthetic"
        assert data["ok"] is True
        assert data["latency_ms"] >= 0

    async def test_probe_all(self, client):
        data = unwrap(await client.post("/api/datasources/probe", json={"capability": "quote"},
                                        timeout=180000))
        assert data["total"] >= 5
        assert "ok_count" in data
        for item in data["items"]:
            assert "name" in item and "ok" in item

    async def test_refresh_data(self, client):
        data = unwrap(await client.post("/api/datasources/refresh",
                                        json={"capability": "snapshot", "force": True},
                                        timeout=300000))
        assert data["items"] > 0
        assert data["source"] == "synthetic"
        assert "cleared_cache_entries" in data

    async def test_reset_breakers(self, client):
        data = unwrap(await client.post("/api/datasources/reset-breakers"))
        assert "reset_breakers" in data

    async def test_endpoints_read(self, client):
        data = unwrap(await client.get("/api/datasources/eastmoney/endpoints"))
        assert data["name"] == "eastmoney"
        assert data["capabilities"]
        assert "kline" in data["urls"]

    async def test_endpoints_write_and_restore(self, client):
        updated = unwrap(await client.put("/api/datasources/tencent/endpoints",
                                          json={"kline": ["https://example.invalid/k"]}))
        assert "kline" in updated["updated"]
        check = unwrap(await client.get("/api/datasources/tencent/endpoints"))
        assert "example.invalid" in check["urls"]["kline"][0]
        # 恢复内置地址
        unwrap(await client.put("/api/datasources/tencent/endpoints", json={"kline": []}))
        check = unwrap(await client.get("/api/datasources/tencent/endpoints"))
        assert all("example.invalid" not in url for url in check["urls"]["kline"])

    async def test_credentials_mask(self, client):
        saved = unwrap(await client.post("/api/datasources/xueqiu/credentials",
                                         json={"cookie": "xq_a_token=abcdefghijklmnopqrstuvwxyz"}))
        assert saved["saved_fields"] == ["cookie"]
        view = unwrap(await client.get("/api/datasources/xueqiu/credentials"))
        masked = view["fields"]["cookie"]
        assert view["configured"] is True
        # 掩码只保留首尾, 中段不可泄露
        assert "*" in masked
        assert "abcdefghijklmn" not in masked
        assert "qrstuvwxyz" not in masked
        cleared = unwrap(await client.delete("/api/datasources/xueqiu/credentials"))
        assert cleared["configured"] is False
        view = unwrap(await client.get("/api/datasources/xueqiu/credentials"))
        assert view["configured"] is False

    async def test_mode_switch(self, client):
        response = await client.post("/api/datasources/mode", json={"mode": "bogus"})
        assert response.status_code == 400
        data = unwrap(await client.post("/api/datasources/mode", json={"mode": "synthetic"}))
        assert data["mode"] == "synthetic"
        assert data["synthetic_allowed"] is True


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
class TestSettingsAPI:
    async def test_get_settings(self, client):
        data = unwrap(await client.get("/api/settings"))
        for group in ("server", "quotas", "push", "news", "scheduler", "data_sources", "auth"):
            assert group in data
        assert "editable_paths" in data
        assert "runtime" in data
        assert data["runtime"]["push_stats"]["configured"] in (True, False)

    async def test_update_whitelist(self, client):
        result = unwrap(await client.put("/api/settings", json={
            "quotas": {"scan_kline_budget": 1500},
            "evil": {"hack": 1},
        }))
        assert "quotas.scan_kline_budget" in result["accepted"]
        assert any(path.startswith("evil") for path in result["rejected"])

    async def test_webhook_masked(self, client):
        unwrap(await client.put("/api/settings",
                                json={"push": {"wecom_webhook": "https://example.invalid/secret-key-123456"}}))
        data = unwrap(await client.get("/api/settings"))
        assert data["push"]["wecom_webhook_configured"] is True
        assert "secret-key-123456" not in data["push"]["wecom_webhook"]
        assert "*" in data["push"]["wecom_webhook"]
        # 清理, 避免影响其它用例
        unwrap(await client.put("/api/settings", json={"push": {"wecom_webhook": ""}}))

    async def test_editable_paths(self, client):
        data = unwrap(await client.get("/api/settings/editable"))
        assert "quotas.universe_size" in data["paths"]
        assert "server.port" in data["restart_required"]

    async def test_export_json(self, client):
        response = await client.get("/api/settings/export")
        assert response.status_code == 200
        assert "attachment" in response.headers.get("content-disposition", "")
        body = response.json()
        assert "runtime" in body or isinstance(body, dict)

    async def test_export_excludes_secrets_by_default(self, client):
        unwrap(await client.put("/api/settings",
                                json={"push": {"wecom_webhook": "https://example.invalid/topsecret"}}))
        response = await client.get("/api/settings/export")
        assert "topsecret" not in response.text
        unwrap(await client.put("/api/settings", json={"push": {"wecom_webhook": ""}}))

    async def test_push_test_without_webhook(self, client):
        response = await client.post("/api/settings/push/test", json={})
        assert response.status_code == 400
        assert "Webhook" in response.json()["message"]

    async def test_push_log(self, client):
        data = unwrap(await client.get("/api/settings/push/log"))
        assert "items" in data and "stats" in data

    async def test_reset_section(self, client):
        unwrap(await client.put("/api/settings", json={"news": {"retention_days": 3}}))
        data = unwrap(await client.post("/api/settings/reset", json={"section": "news"}))
        assert data["news"]["retention_days"] == 15
        response = await client.post("/api/settings/reset", json={"section": "bogus"})
        assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 用户数据
# --------------------------------------------------------------------------- #
class TestUserAPI:
    async def test_watchlist_lifecycle(self, client, warm_market):
        code = warm_market[1].code
        unwrap(await client.post("/api/watchlist", json={"code": code, "name": "测试标的", "note": "单元测试"}))
        data = unwrap(await client.get("/api/watchlist"))
        assert any(item["code"] == code for item in data["items"])
        unwrap(await client.put(f"/api/watchlist/{code}/note", json={"note": "已更新"}))
        data = unwrap(await client.get("/api/watchlist"))
        target = [item for item in data["items"] if item["code"] == code][0]
        assert target["note"] == "已更新"
        removed = unwrap(await client.request("DELETE", "/api/watchlist", json={"codes": [code]}))
        assert removed["removed"] >= 1

    async def test_watchlist_batch(self, client, warm_market):
        codes = [q.code for q in warm_market[2:5]]
        data = unwrap(await client.post("/api/watchlist/batch", json={"codes": codes + ["bad"]}))
        assert len(data["added"]) == len(codes)
        assert data["failed"] == ["bad"]
        unwrap(await client.request("DELETE", "/api/watchlist", json={"codes": codes}))

    async def test_watchlist_invalid_code(self, client):
        response = await client.post("/api/watchlist", json={"code": "abc"})
        assert response.status_code == 400

    async def test_watchlist_note_missing(self, client):
        response = await client.put("/api/watchlist/600519/note", json={"note": "x"})
        assert response.status_code == 404

    async def test_watchlist_pool_has_live_quotes(self, client, warm_market):
        """自选股池必须是「带行情的表格」，而不只是设置页里的代码清单。

        真实问题：自选原先只能在「用户配置」页底部看到，展示字段只有
        代码/名称/备注/移除 —— 没有任何行情，用户加完自选根本不知道
        上哪儿看。现在行情中枢新增「自选股池」页签，这里锁定两条链路：
          1. 批量取行情接口能吃下自选代码集合，且返回真实字段；
          2. 行情缺失时该票仍留在池子里（不能静默消失）。
        """
        codes = [warm_market[0].code, warm_market[1].code]
        unwrap(await client.post("/api/watchlist/batch", json={"codes": codes}))
        try:
            watch = unwrap(await client.get("/api/watchlist"))
            saved = [i["code"] for i in watch["items"]]
            assert set(codes) <= set(saved), f"自选未写入：{saved}"

            quotes = unwrap(await client.get("/api/quotes", params={"codes": ",".join(saved)}))
            items = quotes["items"]
            assert items, "批量行情返回为空"
            by_code = {q["code"]: q for q in items}
            #: 自选池表格要展示的字段必须存在，否则页面只能显示空列
            for field in ("price", "change_pct", "amount", "turnover_rate",
                          "volume_ratio", "amplitude", "total_mv", "board"):
                assert field in items[0], f"行情缺少自选池需要的字段: {field}"
            assert set(by_code) >= set(saved), "有自选代码取不到行情"
        finally:
            await client.request("DELETE", "/api/watchlist", json={"codes": codes})

    async def test_watchlist_has_dedicated_page(self, client):
        """自选与模拟持仓必须独立成页，而不是塞在「用户配置」里。

        真实问题：自选原先只是「用户配置」页底部的一张卡片，展示字段只有
        代码/名称/备注/移除 —— 既没有行情，页面定位也是"改设置"而不是"看盘"。
        现已独立为「我的持仓」页面（#/watch），这里锁定三件事：
          1. 页面已注册、已进导航、脚本已加载；
          2. 自选带实时行情、持仓按现价算浮动盈亏；
          3. 设置页不再重复渲染这份数据，只留入口指引。
        """
        html = (await client.get("/")).text
        assert 'href="#/watch"' in html, "侧边栏缺少「我的持仓」入口"
        assert 'data-nav="watch"' in html, "导航项缺少 data-nav=watch（无法高亮）"
        assert "views/watch.js" in html, "watch.js 未加入脚本加载列表"

        js = (await client.get("/assets/views/watch.js")).text
        assert "SS.views.watch" in js, "未注册 SS.views.watch"
        assert "自选股池" in js and "模拟持仓" in js, "页面缺少两个分区"
        assert "api.quotes" in js, "自选池必须取实时行情"
        assert "market_value" in js and "pnl_pct" in js, "持仓必须计算市值与浮动盈亏"
        assert "data-unwatch" in js, "自选池缺少移除按钮"
        assert "data-close-pos" in js, "持仓缺少平仓按钮"
        assert "moneySigned" in js, "浮盈浮亏必须带符号与涨跌配色"

        #: 行情中枢不再重复提供自选池（避免两处维护同一份数据）
        market = (await client.get("/assets/views/market.js")).text
        assert "自选股池不在这里" in market, "行情中枢应说明自选池已迁走"
        assert "loadWatch" not in market, "行情中枢不应再保留自选池渲染函数"

        #: 设置页只留指引，不再渲染自选/持仓表格
        settings = (await client.get("/assets/views/settings.js")).text
        assert 'href="#/watch"' in settings, "设置页应给出「我的持仓」直达链接"
        for dead in ("paintUserData", "closePositionDialog", "batchWatchDialog", "setUserData"):
            assert dead not in settings, f"设置页残留已迁移的代码: {dead}"

        #: 行内按钮不能触发整行跳转，否则点「移除」会同时打开个股详情
        util = (await client.get("/assets/util.js")).text
        assert "closest('button, a, input, select')" in util, \
            "sortableTable 需忽略行内按钮的点击，避免误跳转"

        #: 加自选后要明确指路，否则用户不知道去哪儿看
        stock = (await client.get("/assets/views/stock.js")).text
        assert "我的持仓" in stock, "「加入自选」的提示应告知自选池位置"

    async def test_bottom_nav_media_query_wins_over_base_rule(self, client):
        """移动端底部导航必须真的显示得出来。

        真实事故：`.bottom-nav` 的基础规则是 `display: none`（桌面隐藏），
        而媒体查询里 `display: flex` 原先被放在**基础规则之前**。两者特异性
        完全相同（0,1,0），CSS 只按源码顺序决胜负 —— 于是 `display:none` 永远胜出，
        **移动端底部导航从未显示过**（媒体查询确实匹配上了，但输在顺序上）。

        坑在于纯靠读代码很难发现：媒体查询存在、写得也对，只是位置不对。
        """
        css = (await client.get("/assets/app.css")).text
        cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

        def pos(pattern):
            m = re.search(pattern, cleaned)
            assert m, f"CSS 里找不到：{pattern}"
            return m.start()

        base = pos(r"\.bottom-nav\s*\{[^}]*display\s*:\s*none")
        mobile = pos(r"@media\s*\(max-width:\s*860px\)\s*\{[^@]*?\.bottom-nav\s*\{[^}]*display\s*:\s*flex")
        assert mobile > base, (
            "`.bottom-nav { display: flex }`（≤860px）必须排在基础规则 "
            "`.bottom-nav { display: none }` 之后，否则同特异性下后者胜出，"
            "移动端底栏不会显示"
        )

        #: 同类风险：窄屏单元格内边距的覆盖也必须晚于基础规则
        base_pad = pos(r"table\.grid th,\s*table\.grid td\s*\{")
        assert pos(r"table\.grid th,\s*table\.grid td\s*\{\s*padding:\s*6px 5px") > base_pad, \
            "窄屏 padding 覆盖必须晚于 table.grid 基础规则，否则不生效"

    async def test_portfolio_lifecycle(self, client, warm_market):
        code = warm_market[3].code
        opened = unwrap(await client.post("/api/portfolio/open", json={
            "code": code, "name": "持仓测试", "price": 10.0, "shares": 100, "reason": "单元测试",
        }))
        assert opened["id"] > 0
        closed = unwrap(await client.post(f"/api/portfolio/{opened['id']}/close",
                                          json={"price": 11.0, "reason": "止盈"}))
        assert closed["pnl_pct"] == pytest.approx(10.0, abs=0.01)
        listing = unwrap(await client.get("/api/portfolio"))
        assert listing["closed_count"] >= 1
        assert listing["win_rate"] >= 0
        unwrap(await client.request("DELETE", f"/api/portfolio/{opened['id']}"))

    async def test_portfolio_invalid_price(self, client):
        response = await client.post("/api/portfolio/open", json={"code": "600519", "price": 0})
        assert response.status_code == 400

    async def test_portfolio_close_missing(self, client):
        response = await client.post("/api/portfolio/999999/close", json={"price": 10})
        assert response.status_code == 400

    async def test_export_signals_csv(self, client):
        response = await client.get("/api/export/signals.csv")
        assert response.status_code == 200
        assert "text/csv" in response.headers.get("content-type", "")
        assert "策略" in response.text

    async def test_export_watchlist_csv(self, client):
        response = await client.get("/api/export/watchlist.csv")
        assert response.status_code == 200
        assert "代码" in response.text

    async def test_export_scan_csv(self, client):
        response = await client.post("/api/export/scan.csv", json={
            "items": [{"code": "600519", "name": "测试", "score": 88, "passed": True,
                       "passed_count": 8, "total_count": 10,
                       "reasons": [{"name": "均线多头", "passed": True}]}],
            "filename": "unit-test.csv",
        })
        assert response.status_code == 200
        assert "均线多头" in response.text


# --------------------------------------------------------------------------- #
# 前端资源
# --------------------------------------------------------------------------- #
class TestFrontendAssets:
    async def test_index_served(self, client):
        response = await client.get("/")
        assert response.status_code == 200
        assert "StockSpace" in response.text
        assert "assets/app.css" in response.text
        assert "no-store" in response.headers.get("cache-control", "")

    async def test_boot_call_matches_exposed_api(self, client):
        """页面调用的启动函数必须真的存在。

        踩过的坑：app.js 把 API 挂在 `SS.app.boot`，而 index.html 调用 `StockSpace.boot()`，
        浏览器抛 "boot is not a function" → 启动函数从未执行 → 页面永久停在"正在加载平台"。
        这里同时校验两边的名字，避免日后又改单边。
        """
        import re

        html = (await client.get("/")).text
        app_js = (await client.get("/assets/app.js")).text

        # 1) 页面里必须有启动调用
        assert "StockSpace.boot()" in html or "StockSpace.app.boot()" in html, "页面没有调用启动函数"

        # 2) 页面调用的名字必须在 app.js 里被真正暴露
        if "StockSpace.boot()" in html:
            assert re.search(r"SS\.boot\s*=", app_js), "页面调用 SS.boot 但 app.js 未暴露它"
        if "StockSpace.app.boot()" in html:
            assert re.search(r"boot:\s*boot", app_js), "页面调用 SS.app.boot 但 app.js 未暴露它"

        # 3) 视图脚本必须全部在启动调用之前加载（否则 boot 时 SS.views 还是空的）
        boot_call = html.rfind("window.StockSpace.boot();")
        assert boot_call > 0, "页面里没有找到启动调用 window.StockSpace.boot();"
        for view in ("dashboard", "market", "emotion", "screener", "stock",
                     "backtest", "datasources", "news", "memory", "settings", "system"):
            marker = f"assets/views/{view}.js"
            assert marker in html, f"缺少视图脚本 {view}"
            assert html.index(marker) < boot_call, f"{view}.js 在启动调用之后加载"

    async def test_static_assets_disable_cache(self, client):
        """静态脚本必须禁止缓存。

        踩过的坑：前端脚本没有内容哈希，而默认静态服务不发 Cache-Control，
        浏览器按启发式规则缓存了旧的 app.js —— 于是"服务端已修复、用户仍看到旧页面"，
        表现为页面一直卡在加载中，极易误判为后端故障。
        """
        for path in ("assets/app.js", "assets/views/dashboard.js", "assets/app.css"):
            response = await client.get("/" + path)
            assert response.status_code == 200, path
            cache_control = response.headers.get("cache-control", "").lower()
            assert "no-store" in cache_control or "no-cache" in cache_control, \
                f"{path} 缺少禁止缓存头（当前: {cache_control!r}）"

    async def test_index_references_versioned_assets(self, client):
        """资源引用应带版本号，升级后能强制拿到新代码。"""
        html = (await client.get("/")).text
        assert "assets/app.js?v=" in html, "assets/app.js 未带版本号查询串"

    async def test_hidden_attribute_actually_hides(self, client):
        """`[hidden]` 必须真正隐藏元素。

        踩过的坑：`hidden` 只是 UA 样式里的 `display:none`，任何作者样式的
        `display` 声明都会覆盖它。`.modal-root { display: grid }` 正是如此 ——
        于是"已隐藏"的弹窗容器依然铺满全屏（z-index:80、pointer-events:auto）：
          * 视觉上像蒙了一层灰雾；
          * 侧边栏、顶部栏、按钮全部点不动。
        这里断言 CSS 里有兜底规则。
        """
        css = (await client.get("/assets/app.css")).text
        assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", css), \
            "缺少 [hidden] { display: none !important } 兜底规则"

    async def test_overlay_containers_start_hidden(self, client):
        """覆盖式容器（弹窗/抽屉遮罩/搜索面板）初始必须带 hidden 属性。"""
        html = (await client.get("/")).text
        for marker in ('id="modalRoot" hidden', 'id="navBackdrop" hidden',
                       'id="searchPanel" hidden', 'id="loadBar" hidden'):
            assert marker in html, f"缺少初始隐藏标记：{marker}"

    async def test_css_fill_rules_have_block_display(self, client):
        """所有"填充/条纹/进度"类图元必须显式 `display: block`。

        踩过的坑：进度条填充元素是 `<i>`，浏览器默认 `display:inline`，
        而 **inline 元素不接受 width/height** —— 于是 `width:27.9%` 与 `height:100%`
        全被忽略，实测尺寸 0x0。表现为「情绪分拆解 / 板块强弱 / 涨停板块聚集」
        三处进度条完全不显示，而 DOM 里元素其实都在：
        接口测试、DOM 存在性检查都发现不了这类问题。

        这类图元有一个共同特征：外层容器负责固定高度，内层负责按百分比填充宽度。
        内层若是 inline，整条进度条就"存在但不可见"。所以这里锁定
        填充类选择器做强制检查。
        """
        css = (await client.get("/assets/app.css")).text
        cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

        #: 这些选择器必须自带 display:block（它们是按百分比填充宽度的图元）
        required = (".bar-fill", ".stack-bar > i", ".progress > i")
        for selector in required:
            pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
            match = re.search(pattern, cleaned)
            assert match, f"CSS 里找不到规则 {selector}"
            body = match.group(1)
            assert re.search(r"display\s*:\s*block", body), \
                f"{selector} 缺少 display:block —— 若元素是 <i>/<span>，宽度百分比会失效（渲染成 0x0）"

        #: 再兜一层：只检查**子元素**图元（带 `>` 或以 -fill 结尾的选择器）。
        #: 父容器（.progress / .bar-track）靠 flex 或固定高度工作，不在检查范围内。
        offenders = []
        for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", cleaned):
            selector = block.group(1).strip()
            body = block.group(2)
            if ":" in selector:
                continue
            tail = selector.rsplit(" ", 1)[-1].strip()
            is_child_figure = (">" in selector or tail.endswith("-fill"))
            if not is_child_figure:
                continue
            if not re.search(r"(bar-fill|stack-bar|progress|bar-track)", selector):
                continue
            if not re.search(r"(^|;)\s*(width|height)\s*:", body):
                continue
            if re.search(r"(^|;)\s*display\s*:", body) or "flex" in body:
                continue
            offenders.append(selector)
        assert not offenders, f"以下填充类子元素规则缺 display，会渲染成 0x0：{offenders}"

    async def test_bar_list_uses_shared_subgrid(self, client):
        """进度条列表必须用三列共享网格，且数值列不得写死宽度。

        踩过的坑（"涨停板块聚集"右侧文字不在一行上）：
          * `.bar-value { width: 74px }` 写死宽度，而该面板的文字是
            "5 家 · 最高 3 板"（实测自然宽 93px）—— 超宽即折成两行；
          * 各行独立 flex，数值长短不一导致轨道终点参差不齐。
        修法是外层 .bar-list 定义列、每行 `grid-template-columns: subgrid` 继承。
        这里同时防住"手写 .bar-row 忘了套 .bar-list"的退化（会退化成整行宽）。
        """
        css = (await client.get("/assets/app.css")).text
        cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

        def rule(selector):
            match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", cleaned)
            assert match, f"CSS 里找不到规则 {selector}"
            return match.group(1)

        assert "subgrid" in rule(".bar-row"), \
            ".bar-row 必须用 grid-template-columns: subgrid 继承 .bar-list 的列宽"
        assert re.search(r"grid-template-columns\s*:", rule(".bar-list")), \
            ".bar-list 必须定义 grid-template-columns 作为共享列模板"
        assert "grid-template-columns" in rule(".card > .bar-row"), \
            "缺少 .card > .bar-row 兜底列模板：手写 .bar-row 漏套 .bar-list 时会退化成整行宽"

        #: 数值列一旦写死宽度，长文本就会被压出折行
        value_rule = rule(".bar-value")
        assert not re.search(r"(?<![-\w])width\s*:", value_rule), \
            f".bar-value 不得写死 width（会导致长数值折行）：{value_rule.strip()}"
        assert not re.search(r"(?<![-\w])max-width\s*:", value_rule), \
            f".bar-value 不得设 max-width（ch 按半角算，中文会被截断）：{value_rule.strip()}"

        #: 渲染端必须真的产出包裹层
        js = (await client.get("/assets/util.js")).text
        assert 'class="bar-list"' in js, "util.barList 必须输出 .bar-list 包裹层"

    async def test_data_tables_are_real_tables(self, client):
        """数据表必须是真正的 table，不能被布局用的 `.grid` 类改写格式化上下文。

        真实事故（范围极大、且此前的检查全部漏掉）：布局类 ``.grid { display: grid }``
        （配合 .cols-2/.cols-3/.kpi）与数据表的类名 ``<table class="grid">`` **命名冲突**，
        于是全站 24 个数据表都变成了 CSS Grid 容器 —— auto table layout 根本没机会运行，
        每个 <th>/<td> 退化成按 max-content 收缩的 grid item，所有列挤在左侧。

        实测（1440px 视口，榜单速览卡宽 1204px）：
            修复前 列宽 40/42/42/42/54/54 = 274px，只用 23% 宽度，右侧空约 900px
            修复后 列宽 40/211/246/196/240/240 = 1174px，铺满 100%
        ``table-layout: fixed`` 对此无效（display:grid 直接改写格式化上下文）。
        """
        css = (await client.get("/assets/app.css")).text
        cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

        #: 必须有一条把数据表还原成 display:table 的规则
        table_rule = re.search(r"table\.grid\s*\{([^}]*)\}", cleaned)
        assert table_rule, "缺少 table.grid 规则"
        assert re.search(r"display\s*:\s*table", table_rule.group(1)), \
            "table.grid 必须显式 display:table —— 否则会被布局类 .grid 的 display:grid 顶掉"

        #: 兜底：不允许任何以 .grid 结尾的规则把表格设成 grid/flex
        for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", cleaned):
            selector = block.group(1).strip()
            if "table" in selector:
                continue
            if not re.search(r"\.grid\s*$", selector):
                continue
            body = block.group(2)
            if re.search(r"display\s*:\s*(grid|flex)", body):
                assert "min(" in body or "cols-" in selector or selector == ".grid", \
                    f"规则 {selector} 的 display:{body.strip()[:40]} 可能再次覆盖数据表"

        #: 页面上的数据表确实都带 grid 类（这正是冲突的来源，需长期盯住）
        for page in ("/assets/views/dashboard.js", "/assets/views/emotion.js",
                     "/assets/views/market.js", "/assets/util.js"):
            source = (await client.get(page)).text
            assert '<table class="grid"' in source, f"{page} 里的数据表应使用 table.grid"

    async def test_util_progress_skips_zero_fill(self, client):
        """值为 0 时不应渲染 0 宽度的填充元素（避免出现无效的 0x0 节点）。"""
        js = (await client.get("/assets/util.js")).text
        assert "ratio > 0" in js, "util.progress 应跳过 0 值的填充元素"

    async def test_no_absolute_urls_in_html(self, client):
        html = (await client.get("/")).text
        # 页面不得引用任何外部域名(CDN)
        for marker in ("//cdn.", "https://unpkg.com", "https://cdn.jsdelivr.net", "http://cdn."):
            assert marker not in html
        # 所有 script/link 必须是相对路径
        import re

        for match in re.finditer(r'(?:src|href)="([^"]+)"', html):
            target = match.group(1)
            assert not target.startswith("http"), f"发现绝对地址: {target}"
            assert not target.startswith("//"), f"发现协议相对地址: {target}"

    @pytest.mark.parametrize("path", [
        "assets/app.css", "assets/app.js", "assets/api.js", "assets/util.js",
        "assets/charts.js", "assets/favicon.svg",
        "assets/views/dashboard.js", "assets/views/market.js", "assets/views/emotion.js",
        "assets/views/screener.js", "assets/views/stock.js", "assets/views/backtest.js",
        "assets/views/datasources.js", "assets/views/news.js", "assets/views/memory.js",
        "assets/views/settings.js", "assets/views/system.js",
    ])
    async def test_static_assets(self, client, path):
        response = await client.get("/" + path)
        assert response.status_code == 200, path
        assert len(response.content) > 50, path

    async def test_selfcheck_page(self, client):
        response = await client.get("/selfcheck")
        assert response.status_code == 200
        assert "部署自检" in response.text

    async def test_manifest(self, client):
        response = await client.get("/manifest.webmanifest")
        assert response.status_code == 200

    async def test_docs_available(self, client):
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert len(spec["paths"]) >= 60


# --------------------------------------------------------------------------- #
# 响应信封与错误处理
# --------------------------------------------------------------------------- #
class TestEnvelope:
    async def test_envelope_shape(self, client):
        payload = (await client.get("/api/health")).json()
        assert set(payload.keys()) >= {"code", "message", "data", "ts"}
        assert payload["code"] == 0
        assert payload["message"] == "ok"

    async def test_404_returns_json(self, client):
        response = await client.get("/api/definitely-not-exists")
        assert response.status_code == 404
        assert "application/json" in response.headers.get("content-type", "")

    async def test_validation_error_is_json(self, client):
        response = await client.get("/api/market/rank", params={"limit": 9999})
        assert response.status_code == 422
        assert "application/json" in response.headers.get("content-type", "")
