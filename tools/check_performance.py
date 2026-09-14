"""验证历史绩效复盘：造一批"过去某日的扫描结果"，跑真实回放。

为什么需要这样造数据：真实 scan_result 只有今天一天，而"今天"恰好是行情数据的
最后一根 —— 严格按"入场后第 N 根"回放时没有任何后续 bar 可用。
这正是**前向积累**的固有约束（build_market_context 只认当日快照，无法回填历史扫描）。
所以这里插入一条 2026-08-03 的扫描记录，用**真实的日线行走**验证回放逻辑。

脚本默认只打印；加 --apply 才写入，跑完会自行清理。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from stock_space.core.http import http_client  # noqa: E402
from stock_space.store.db import db  # noqa: E402

SCAN_DATE = "2026-08-03"
STRATEGY = "volume_shrink_rebound"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--codes", default="", help="逗号分隔；默认自动挑 12 只")
    args = parser.parse_args()

    logging.disable(logging.WARNING)
    db.init()
    await http_client.start()
    from stock_space.services import performance_service as pf
    from stock_space.store.kline_store import kline_store

    #: 挑一批"在 SCAN_DATE 之前就有足够历史、且其后续有真实行情"的标的
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if not codes:
        from stock_space.providers.registry import registry

        snapshot = await registry.snapshot()
        for quote in sorted(snapshot, key=lambda q: -q.amount):
            kline = kline_store.get(quote.code, 400)
            if kline is None or not kline.bars:
                continue
            dates = [str(b.date) for b in kline.bars]
            if dates[0] <= SCAN_DATE <= dates[-1] and len(dates) >= 60:
                codes.append(quote.code)
            if len(codes) >= 12:
                break

    print(f"用 {len(codes)} 只标的在 {SCAN_DATE} 造扫描记录: {', '.join(codes[:6])} …")
    if not args.apply:
        print("（预演）加 --apply 才写入")
        await http_client.close()
        return 0

    payload = json.dumps({"strategy": STRATEGY, "code": codes[0], "score": 80.0,
                          "reasons": [], "metrics": {}, "tags": []}, ensure_ascii=False)
    with db.transaction() as conn:
        conn.execute("DELETE FROM scan_result WHERE strategy=? AND trade_date=?",
                     (STRATEGY, SCAN_DATE))
        for rank, code in enumerate(codes, start=1):
            item = json.loads(payload)
            item["code"] = code
            conn.execute(
                "INSERT INTO scan_result(strategy, trade_date, code, name, score, rank, "
                "payload, source, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (STRATEGY, SCAN_DATE, code, "", 80.0 - rank, rank,
                 json.dumps(item, ensure_ascii=False), "verify", 0.0),
            )
    print("已写入，开始回放…")

    result = await pf.review_scan_history(STRATEGY, limit_days=60)
    m = result["metrics"]
    print()
    print("扫描 %s | 回放 %s | 跳过 %s (无日线 %s / 无后续行情 %s)" % (
        result["scanned"], len(result["trades"]), result["skipped"],
        result.get("skipped_no_data"), result.get("skipped_no_future_bars")))
    print("胜率 %.1f%% | 盈亏比 %s | 盈利因子 %s | 期望 %+.2f%% | 平均持仓 %.1f 日" % (
        (m.get("win_rate") or 0) * 100, m.get("payoff_ratio"), m.get("profit_factor"),
        m.get("expectancy_pct") or 0, m.get("avg_hold_days") or 0))
    print()
    print("逐笔:")
    for t in result["trades"]:
        print("  %s 入%s@%.2f -> 出%s@%.2f %+.2f%% 持%sd %s" % (
            t["code"], t["entry_date"], t["entry_price"], t["exit_date"],
            t["exit_price"], t["pnl_pct"], t["hold_days"], t["exit_reason"]))
    print()
    print("按离场原因:")
    for row in result["attribution"].get("by_exit_reason", []):
        print("  %-12s n=%-3s 胜率%4.0f%% 盈亏比%-7s 均值%+.2f%%" % (
            row["label"], row["count"], row["win_rate"] * 100,
            row["payoff_ratio"] if row["payoff_ratio"] is not None else "--",
            row["avg_pnl_pct"]))
    print()
    print("建议:")
    for s in result["suggestions"]:
        print("  [%s] %s — %s" % (s["level"], s["title"], s["detail"][:100]))

    # 清理
    with db.transaction() as conn:
        conn.execute("DELETE FROM scan_result WHERE strategy=? AND trade_date=?",
                     (STRATEGY, SCAN_DATE))
    print()
    print("已清理造的扫描记录")
    await http_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
