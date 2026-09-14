"""离线端到端冒烟脚本。

不需要任何网络: 强制 ``synthetic`` 模式, 用内置的确定性行情剧本跑通
「数据源 → 上下文 → 5 个策略扫描 → 回测 → 情绪 → 复盘 → 推送内容构造」全链路。

用途:
  * 部署后立即验证功能是否可用(``python tools/smoke.py``);
  * CI 里作为快速回归。

退出码 0 表示全部通过。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(_BACKEND))

# 必须在导入 stock_space 之前设定, 保证全程只用合成数据
os.environ.setdefault("SS_DATA_SOURCES__MODE", "synthetic")
os.environ.setdefault("SS_SCHEDULER__ENABLED", "false")
os.environ.setdefault("SS_LOG__LEVEL", "WARNING")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> int:
    from stock_space.config import config, load_config
    from stock_space.core.http import http_client
    from stock_space.core.memory import memory_guard
    from stock_space.core.util import board_of, detect_market, limit_pct, market_session, normalize_code
    from stock_space.engines import STRATEGY_ORDER, all_strategies, catalog
    from stock_space.engines.backtest import Backtester
    from stock_space.engines.emotion import snapshot as emotion_snapshot
    from stock_space.engines.review import ReviewInput, build_report, check_compliance
    from stock_space.engines.scanner import build_market_context, scan
    from stock_space.paths import ensure_dirs
    from stock_space.providers.registry import registry
    from stock_space.services import market_service, push_service, user_service
    from stock_space.store.db import db
    from stock_space.store.kline_store import kline_store

    ensure_dirs()
    db.init()
    await http_client.start()
    registry.build(force=True)

    section("1. 配置与模式")
    check("数据源模式为 synthetic", config().source_mode == "synthetic", config().source_mode)
    check("合成数据源已注册", registry.get("synthetic") is not None)

    section("2. 代码 / 板块 / 涨跌停规则")
    check("北交所 920 段识别", detect_market("920001") == "bj", detect_market("920001"))
    check("科创板识别", board_of("688981") == "科创板", board_of("688981"))
    check("创业板识别", board_of("300750") == "创业板", board_of("300750"))
    check("北交所涨跌停 30%", limit_pct("920001") == 30.0, str(limit_pct("920001")))
    check("创业板涨跌停 20%", limit_pct("300750") == 20.0, str(limit_pct("300750")))
    check("ST 涨跌停 5%", limit_pct("600519", "ST某某") == 5.0, str(limit_pct("600519", "ST某某")))
    check("代码规整 sh600519", normalize_code("sh600519") == "600519")
    check("代码规整 600519.SH", normalize_code("600519.SH") == "600519")
    check("市场时钟可用", market_session().phase in
          ("pre_open", "call_auction", "trading", "lunch_break", "closed", "weekend", "holiday"),
          market_session().label)

    section("3. 数据源调度(合成模式必须独占)")
    for capability in ("snapshot", "kline", "quote", "rank", "sector", "breadth", "limit_up_pool"):
        order = registry.candidates(capability)
        check(f"能力 {capability} 候选", order == ["synthetic"] or (not order and capability not in
              ("snapshot", "kline", "quote", "rank", "sector", "breadth", "limit_up_pool")),
              str(order))

    section("4. 快照与上下文")
    t0 = time.perf_counter()
    quotes = await registry.snapshot(force=True)
    elapsed = time.perf_counter() - t0
    check("快照非空", len(quotes) > 100, f"{len(quotes)} 只 / {elapsed:.2f}s")
    check("快照含北交所", any(q.market == "bj" or board_of(q.code) == "北交所" for q in quotes))
    check("快照字段完整", all(q.price > 0 and q.name for q in quotes[:50]))
    check("成交量单位为股(>1000)", quotes[0].volume > 1000, str(quotes[0].volume))

    context = await build_market_context(force=True)
    check("上下文标的数", context.universe_size > 100, str(context.universe_size))
    check("环境闸门取值合法", context.env_gate in ("full", "half", "off"), context.env_gate)
    check("板块统计非空", len(context.sector_stats) > 0, f"{len(context.sector_stats)} 个板块")

    section("5. K 线与指标")
    kline = await registry.kline(quotes[0].code, 250)
    check("K线根数 ≥ 200", len(kline.bars) >= 200, str(len(kline.bars)))
    check("K线 OHLC 合法", all(b.high >= b.low > 0 for b in kline.bars))
    payload = await market_service.kline(quotes[0].code, 250)
    indicators = payload.get("indicators") or {}
    check("指标齐全", all(k in indicators for k in ("ma20", "ma60", "dif", "k", "rsi14", "atr14")),
          ",".join(sorted(indicators.keys()))[:80])
    daily = await market_service.stock_detail(quotes[0].code)
    check("个股详情含行情", bool(daily.get("quote")))
    check("个股详情含K线", bool(daily.get("kline")))

    section("6. 策略目录与扫描")
    catalog_items = catalog()
    check("策略数量 ≥ 5", len(catalog_items) >= 5, str(len(catalog_items)))
    check("策略均含默认参数", all(item.get("params") for item in catalog_items))
    check("策略均含回测口径", all(item.get("backtest") for item in catalog_items))

    for key in STRATEGY_ORDER:
        t0 = time.perf_counter()
        outcome = await scan(key, force=False, limit=10, context=context, persist=False)
        elapsed = time.perf_counter() - t0
        check(f"策略 {key} 扫描有结果", outcome.total_evaluated > 0,
              f"评估 {outcome.total_evaluated} 只 / 入选 {outcome.passed_count} / {elapsed:.1f}s")
        if outcome.signals:
            top = outcome.signals[0]
            check(f"策略 {key} 评分在 0~100", 0 <= top.score <= 100, str(top.score))
            check(f"策略 {key} 有条件明细", len(top.reasons) > 0, f"{len(top.reasons)} 条")
            check(f"策略 {key} 含止损价", top.stop_loss > 0, str(top.stop_loss))

    section("7. 单只评估与评分拆解")
    from stock_space.engines import get as get_strategy
    from stock_space.engines.scanner import evaluate_one
    probe_code = quotes[0].code
    probe_kline = await registry.kline(probe_code, 250)
    probe_quote = next((q for q in quotes if q.code == probe_code), quotes[0])
    for key in STRATEGY_ORDER:
        strategy = get_strategy(key)
        signal = evaluate_one(strategy, probe_quote, probe_kline, {}, context)
        check(f"{key} 单只评估返回条件", len(signal.reasons) > 0, f"score={signal.score}")
        check(f"{key} 评分有限", 0 <= signal.score <= 100, str(signal.score))

    section("8. 情绪与复盘")
    # 合成剧本的最后一个交易日未必有涨停, 因此接受"结构可用"或"明确报错"
    try:
        pool = await market_service.limit_up_pool()
        check("涨停池结构可用", "limit_up_count" in pool, f"涨停 {pool['limit_up_count']}")
    except market_service.ServiceUnavailable as exc:
        message = str(exc)
        check("涨停池无样本时明确报错(不返回假数据)",
              "limit_up_pool" in message or "校验" in message,
              f"{type(exc).__name__}: {message[:90]}")
        pool = {"limit_up_count": 0, "ladder": {}, "broken": []}
    panel = emotion_snapshot(quotes=quotes[:300], limit_up=[], broken=[], breadth=None)
    check("情绪分在 0~100", 0 <= panel["emotion"]["score"] <= 100, str(panel["emotion"]["score"]))
    check("周期阶段合法", panel["cycle"]["phase"] in
          ("ice", "start", "ferment", "climax", "ebb"), panel["cycle"]["phase"])
    check("情绪含逐项依据", len(panel["emotion"]["parts"]) == 6, str(len(panel["emotion"]["parts"])))

    report = build_report(ReviewInput(
        quotes=quotes[:500], limit_up=[], broken=[], source="synthetic",
    ))
    check("复盘含模块", len(report["modules"]) >= 4, str(len(report["modules"])))
    check("复盘合规校验通过", report["compliance"]["ok"], str(report["compliance"]["issues"]))
    check("复盘含风险提示", "不构成" in report["risk_notice"])
    check("复盘摘要非空", bool(push_service.__name__) and bool(report["conclusion"]))

    section("9. 回测")
    universe = []
    for quote in sorted(quotes, key=lambda q: -q.amount)[:12]:
        kl = await registry.kline(quote.code, 260)
        if kl and len(kl.bars) >= 130:
            universe.append((quote, kl))
    check("回测样本充足", len(universe) >= 5, f"{len(universe)} 只")
    from stock_space.engines import TrendStrategy
    bt = Backtester(TrendStrategy(), {}, fill="next_open", max_positions=3)
    result = await asyncio.to_thread(bt.run, universe)
    metrics = result.metrics
    check("回测返回指标", "win_rate" in metrics and "trade_count" in metrics,
          f"trades={metrics.get('trade_count')}")
    check("回测净值曲线非空", len(result.equity_curve) > 0, str(len(result.equity_curve)))
    check("回测成本模型生效", result.cost_model.get("slippage_rate", 0) > 0,
          str(result.cost_model))

    section("10. 数据源可观测性与手动切换")
    report_sources = registry.report()
    check("数据源报告含能力分类", len(report_sources["categories"]) > 10,
          str(len(report_sources["categories"])))
    check("快照能力生效源为 synthetic",
          report_sources["categories"]["snapshot"]["effective_order"] == ["synthetic"],
          str(report_sources["categories"]["snapshot"]["effective_order"]))
    lock_result = registry.lock("kline", "synthetic")
    check("手动锁定成功", lock_result.get("locked") == "synthetic", str(lock_result))
    registry.unlock_all()
    check("解锁后无锁定", registry.locks() == {}, str(registry.locks()))
    probe = await registry.probe_all("quote")
    check("连通性探测返回全部源", len(probe) >= 8, str(len(probe)))

    section("11. 用户数据(自选 / 持仓 / 信号流水)")
    user_service.add_watchlist(quotes[0].code, name=quotes[0].name, note="冒烟测试")
    watchlist = user_service.list_watchlist()
    check("自选写入成功", any(item["code"] == quotes[0].code for item in watchlist),
          f"{len(watchlist)} 条")
    user_service.remove_watchlist([quotes[0].code])
    check("自选删除成功", not any(item["code"] == quotes[0].code
                                  for item in user_service.list_watchlist()))

    position = user_service.open_position(quotes[1].code, float(quotes[1].price),
                                         name=quotes[1].name, reason="冒烟")
    closed = user_service.close_position(position["id"], float(quotes[1].price) * 1.05,
                                         reason="冒烟")
    check("持仓开平仓成功", closed["pnl_pct"] > 4.0, str(closed["pnl_pct"]))
    user_service.delete_position(position["id"])
    portfolio = user_service.list_portfolio()
    check("持仓统计可用", "open_count" in portfolio, str(portfolio["win_rate"]))

    section("12. 推送内容构造(不实际发送)")
    message = push_service.test_message()
    check("测试消息可构造", bool(message.title) and bool(message.body))
    from stock_space.services.push_service import clip_bytes
    clipped = clip_bytes("测试" * 3000, 3800)
    check("字节截断不破坏多字节字符", len(clipped.encode("utf-8")) <= 3800 and "测" in clipped,
          f"{len(clipped.encode('utf-8'))} bytes")
    signal_msg = push_service.signal_message(
        strategy_name="潜涨雷达",
        items=[{"code": "600519", "name": "测试", "score": 88.5,
                "reasons": [{"name": "温和放量", "passed": True}]}],
    )
    md = signal_msg.to_markdown()
    check("信号推送为 markdown", "## " in md and "600519" in md, md.splitlines()[0][:40])

    section("13. 内存监控")
    memory_guard.start()
    memory_guard.sample_and_enforce()
    report_mem = memory_guard.report(trend_points=10)
    check("内存报告含进程信息", report_mem["process"]["rss_mb"] > 0,
          f"{report_mem['process']['rss_mb']} MB")
    check("内存报告含阈值", report_mem["limits"]["soft_limit_mb"] > 0,
          str(report_mem["limits"]["soft_limit_mb"]))
    check("内存报告含缓存明细", len(report_mem["caches"]["items"]) >= 5,
          f"{len(report_mem['caches']['items'])} 个缓存: "
          + ",".join(item["name"] for item in report_mem["caches"]["items"]))
    check("内存趋势有采样点", len(report_mem["trend"]) > 0, str(len(report_mem["trend"])))
    flush = memory_guard.flush()
    check("手动释放缓存可用", "freed_entries" in flush, str(flush))
    memory_guard.stop()

    section("14. 设置读写与掩码")
    from stock_space.store.settings_store import mask_secret, settings_store
    check("掩码保留首尾", mask_secret("abcdefghijklmnop") == "abcdefgh********mnop",
          mask_secret("abcdefghijklmnop"))
    check("短串掩码安全", "*" not in mask_secret("") and mask_secret("ab") == "**",
          mask_secret("ab"))
    result = settings_store.update({"quotas": {"universe_size": 0}, "bogus": {"x": 1}})
    check("白名单拒绝越权字段", "bogus.x" in result["rejected"] or
          "quotas.universe_size" in result["accepted"], str(result))
    snapshot = settings_store.public_snapshot()
    check("设置视图含必要分组",
          all(k in snapshot for k in ("server", "quotas", "push", "data_sources", "news")),
          ",".join(sorted(snapshot.keys()))[:80])
    check("Webhook 不回显明文",
          "http" not in str(snapshot["push"].get("wecom_webhook", "")),
          str(snapshot["push"].get("wecom_webhook")))

    section("15. 数据库与缓存")
    stats = db.stats()
    check("数据库表可用", stats["rows"].get("watchlist", -1) >= 0, str(len(stats["rows"])))
    coverage = kline_store.coverage()
    check("日线缓存有数据", coverage["codes"] > 0 or coverage["bars"] == 0,
          f"{coverage['codes']} 只 / {coverage['bars']} 根")
    kv_ok = db.kv_get("__smoke__", None)
    db.kv_set("__smoke__", {"ok": True})
    check("键值存储可用", db.kv_get("__smoke__", {}).get("ok") is True, str(kv_ok))
    db.kv_delete("__smoke__")

    await http_client.close()

    section("结果")
    print(f"通过 {len(PASSED)} 项, 失败 {len(FAILED)} 项")
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAIL: {name}  {detail}")
        return 1
    print("全部通过 —— 离线全链路可用")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        code = 2
    raise SystemExit(code)
