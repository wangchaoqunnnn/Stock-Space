"""清理 kline_daily 中的合成(演示)数据 —— 一次性维护脚本。

背景：演示模式下抓取的假K线被写进了共享磁盘缓存并长期留存，
之后真实模式下被当作真实日线返回（分众传媒事件）。两道闸已在
kline_store 里加好（put 防新增 / _read_disk 防既有），本脚本负责
把历史遗留的污染数据真正删掉，让它们重新走真实数据源。

默认只报告不删除；加 --apply 才执行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from stock_space.config import load_config  # noqa: E402
from stock_space.store.db import db  # noqa: E402
from stock_space.store.kline_store import SYNTHETIC_SOURCE, kline_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="清理日线缓存中的合成数据")
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认只报告）")
    args = parser.parse_args()

    load_config(reload=True)
    db.init()

    rows = db.query(
        "SELECT code, COUNT(*) n FROM kline_daily WHERE source LIKE ? GROUP BY code "
        "ORDER BY code",
        (f"{SYNTHETIC_SOURCE}%",),
    )
    if not rows:
        print("没有发现合成数据，无需清理。")
        return 0

    codes = [r["code"] for r in rows]
    total_rows = sum(int(r["n"]) for r in rows)
    print("发现合成(演示)日线：%d 只股票 / %d 行" % (len(codes), total_rows))
    print("示例:", ", ".join(codes[:12]), "…" if len(codes) > 12 else "")

    if not args.apply:
        print()
        print("这是**预演**。加 --apply 才会真正删除，例如：")
        print("  python tools/purge_demo_kline.py --apply")
        return 0

    conn = db.connection
    with db.transaction() as conn:
        conn.execute("DELETE FROM kline_daily WHERE source LIKE ?", (f"{SYNTHETIC_SOURCE}%",))
        #: 抓取日志也要一并清掉，否则会被 has_fresh() 当成"今日已抓过"而跳过重抓
        conn.execute("DELETE FROM kline_fetch_log WHERE source LIKE ?", (f"{SYNTHETIC_SOURCE}%",))
    kline_store.cache.clear()
    print("已删除 %d 只股票的合成日线，并清空内存缓存。" % len(codes))
    print("下次请求这些标的时会重新走真实数据源。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
