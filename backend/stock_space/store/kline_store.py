"""日线磁盘缓存。

设计动机(实测数据): 免费行情源会对高频请求封禁 IP(新浪 HTTP 456 / 腾讯 HTTP 501),
而全市场 5000+ 只股票从 SQLite 重建一次约 40 秒 —— 如果每次都走网络, 服务无法使用。

三级读取:
    内存缓存(``kline_cache``)  →  磁盘(本模块)  →  上游数据源

写入策略: 按 ``(code, date)`` 主键 UPSERT, 一个批次合并为单事务
(逐只 commit 会让全市场写回从 1 秒变成几十秒)。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Iterable, Sequence

from ..core.cache import BoundedTTLCache
from ..core.memory import memory_guard
from ..core.util import today_str
from ..config import config
from ..models import Bar, KLine
from .db import db

logger = logging.getLogger(__name__)

#: 同一交易日内不重复向网络请求同一只股票的日线
FETCH_FRESH_SECONDS = 6 * 3600

#: 合成(演示)数据的来源标记 —— 见 KLineStore._reject_demo()
SYNTHETIC_SOURCE = "synthetic"


class KLineStore:
    """日线落盘缓存。"""

    def __init__(self) -> None:
        self._cache: BoundedTTLCache[KLine] = BoundedTTLCache(
            name="kline_memory", max_entries=1200, ttl_seconds=900.0
        )
        memory_guard.register(self._cache)
        self._lock = threading.RLock()
        self._stats = {"memory_hits": 0, "disk_hits": 0, "network": 0, "writes": 0}
        self._warned: set[str] = set()

    # ------------------------------ 读 ------------------------------
    def get(self, code: str, days: int = 260, *, allow_disk: bool = True) -> KLine | None:
        key = f"{code}:{days}"
        cached = self._cache.get(key)
        if cached is not None:
            self._stats["memory_hits"] += 1
            return cached
        if not allow_disk:
            return None
        kline = self._read_disk(code, days)
        if kline is not None:
            self._stats["disk_hits"] += 1
            self._cache.set(key, kline)
        return kline

    def _read_disk(self, code: str, days: int) -> KLine | None:
        try:
            rows = db.query(
                "SELECT trade_date, open, high, low, close, volume, amount, change_pct, "
                "turnover_rate, source FROM kline_daily WHERE code=? "
                "ORDER BY trade_date DESC LIMIT ?",
                (code, max(1, days)),
            )
        except sqlite3.Error as exc:
            logger.warning("读取日线失败 %s: %s", code, exc)
            return None
        if not rows:
            return None
        #: 第二道闸：即使库里已经存在演示数据(历史遗留)，非演示模式也不得把它当真实数据。
        #: 两道闸是互补的 —— put() 防新增，这里防既有；缺任何一个都会让假K线重新露头。
        disk_source = str(rows[0]["source"] or "disk")
        if self._is_demo(disk_source) and not config().synthetic_allowed:
            marker = f"demo-serve:{code}"
            if marker not in self._warned:
                self._warned.add(marker)
                logger.warning(
                    "跳过磁盘缓存中的合成K线: %s (source=%s) —— 改走真实数据源；"
                    "如确认无用可执行 kline_store.clear(code=%r) 清除",
                    code, disk_source, code,
                )
            return None
        bars = [
            Bar(
                date=str(row["trade_date"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"] or 0),
                amount=float(row["amount"] or 0),
                change_pct=float(row["change_pct"] or 0),
                turnover_rate=float(row["turnover_rate"] or 0),
            )
            for row in reversed(rows)
        ]
        return KLine(code=code, period="day", bars=bars, source=disk_source)

    def has_fresh(self, code: str) -> bool:
        """当日是否已成功抓取过(用于跳过网络请求)。"""
        try:
            row = db.query_one(
                "SELECT fetched_at, last_date FROM kline_fetch_log WHERE code=?", (code,)
            )
        except sqlite3.Error:
            return False
        if row is None:
            return False
        fetched_at = float(row["fetched_at"] or 0)
        if time.time() - fetched_at > FETCH_FRESH_SECONDS:
            return False
        last_date = str(row["last_date"] or "")
        # 盘中当天的最后一根 K 线可能仍在变动, 收盘前允许刷新
        return last_date >= today_str() or time.time() - fetched_at < 1800

    # ------------------------------ 写 ------------------------------
    def _is_demo(self, source: str) -> bool:
        return str(source or "").strip().lower().startswith(SYNTHETIC_SOURCE)

    def put(self, kline: KLine, *, source: str = "") -> int:
        if not kline or not kline.bars:
            return 0
        origin = source or kline.source or ""
        #: ⚠️ 演示(合成)数据绝不写入共享磁盘缓存 —— 除非整个服务就运行在演示模式。
        #:
        #: 真实事故：某次以 synthetic 模式跑过之后，320 只股票的假K线被 UPSERT 进
        #: `kline_daily` 并长期留存；之后 auto/real 模式下 `_read_disk()` 命中这些行，
        #: **不看 source 就直接当真实数据返回** —— 于是个股页出现"分众传媒(真实价 4.74)
        #: 配 621 元的假K线"，均线/技术指标/止损价全部基于假序列计算。
        #: 磁盘缓存是"跨进程、跨模式"共享的，混入演示数据等于永久污染。
        if self._is_demo(origin) and not config().synthetic_allowed:
            logger.warning(
                "拒绝把合成K线写入磁盘缓存: %s (source=%s) —— 演示数据不得污染真实缓存",
                kline.code, origin,
            )
            return 0
        rows = [
            (
                kline.code, bar.date, bar.open, bar.high, bar.low, bar.close,
                bar.volume, bar.amount, bar.change_pct, bar.turnover_rate,
                "qfq", origin, time.time(),
            )
            for bar in kline.bars
        ]
        try:
            with db.transaction() as conn:
                conn.executemany(
                    "INSERT INTO kline_daily(code, trade_date, open, high, low, close, volume, "
                    "amount, change_pct, turnover_rate, adj, source, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(code, trade_date) DO UPDATE SET "
                    "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
                    "volume=excluded.volume, amount=excluded.amount, change_pct=excluded.change_pct, "
                    "turnover_rate=excluded.turnover_rate, source=excluded.source, "
                    "updated_at=excluded.updated_at",
                    rows,
                )
                conn.execute(
                    "INSERT INTO kline_fetch_log(code, fetched_at, source, bars, last_date) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
                    "fetched_at=excluded.fetched_at, source=excluded.source, "
                    "bars=excluded.bars, last_date=excluded.last_date",
                    (
                        kline.code, time.time(), origin,
                        len(kline.bars), kline.bars[-1].date,
                    ),
                )
        except sqlite3.Error as exc:
            # 逐只股票写失败会刷屏(例如数据库尚未初始化), 每个错误信息只报一次
            marker = str(exc)[:80]
            if marker not in self._warned:
                self._warned.add(marker)
                logger.warning("写入日线失败(%s): %s", kline.code, exc)
            return 0

        self._stats["writes"] += len(rows)
        self._cache.set(f"{kline.code}:{len(kline.bars)}", kline)
        return len(rows)

    def put_many(self, klines: Iterable[KLine], *, source: str = "") -> int:
        """批量写入(合并为逐只事务, 已经是"一次事务多行"的形态)。"""
        total = 0
        for kline in klines:
            total += self.put(kline, source=source)
        return total

    # ------------------------------ 周期聚合 ------------------------------
    def get_period(self, code: str, days: int = 260, period: str = "day") -> KLine | None:
        """按周期读取。

        磁盘里只保存**日线**这一种原始数据 —— 周线与月线都由日线本地聚合得到。
        这样做的好处: 只有一个口径需要维护, 也不会因为"周线缓存与日线缓存不一致"
        而产生策略信号冲突。
        """
        if period in ("", "day"):
            return self.get(code, days)
        daily_days = {"week": min(1200, days * 6), "month": min(1200, days * 22)}.get(period, days)
        daily = self.get(code, daily_days)
        if daily is None or not daily.bars:
            return None
        bars = aggregate_bars(daily.bars, period)
        if not bars:
            return None
        return KLine(code=daily.code, name=daily.name, period=period,
                     bars=bars[-days:], source=f"{daily.source}+{period}", fetched_at=daily.fetched_at)

    # ------------------------------ 维护 ------------------------------
    def coverage(self) -> dict[str, Any]:
        try:
            row = db.query_one(
                "SELECT COUNT(DISTINCT code) AS codes, COUNT(*) AS bars, "
                "MIN(trade_date) AS first_date, MAX(trade_date) AS last_date FROM kline_daily"
            )
            fresh = db.query_one(
                "SELECT COUNT(*) AS n FROM kline_fetch_log WHERE fetched_at > ?",
                (time.time() - FETCH_FRESH_SECONDS,),
            )
        except sqlite3.Error:
            return {"codes": 0, "bars": 0, "fresh_today": 0}
        return {
            "codes": int(row["codes"] or 0) if row else 0,
            "bars": int(row["bars"] or 0) if row else 0,
            "first_date": str(row["first_date"] or "") if row else "",
            "last_date": str(row["last_date"] or "") if row else "",
            "fresh_codes": int(fresh["n"] or 0) if fresh else 0,
        }

    def clear(self, *, code: str | None = None) -> int:
        try:
            if code:
                cur = db.execute("DELETE FROM kline_daily WHERE code=?", (code,))
                db.execute("DELETE FROM kline_fetch_log WHERE code=?", (code,))
            else:
                cur = db.execute("DELETE FROM kline_daily")
                db.execute("DELETE FROM kline_fetch_log")
        except sqlite3.Error:
            return 0
        self._cache.clear()
        return cur.rowcount or 0

    # ------------------------------ 缓存协议 ------------------------------
    @property
    def cache(self) -> BoundedTTLCache[KLine]:
        return self._cache

    def stats(self) -> dict[str, Any]:
        return {
            "memory_cache": self._cache.stats().as_dict(),
            "disk": self.coverage(),
            "counters": dict(self._stats),
        }


kline_store = KLineStore()


# --------------------------------------------------------------------------- #
# 日线 → 周线 / 月线 聚合
# --------------------------------------------------------------------------- #
def aggregate_bars(bars: Sequence[Bar], period: str) -> list[Bar]:
    """把日线聚合成周线或月线。

    聚合规则(与主流软件一致):
      * 开盘取区间第一根的开盘, 收盘取最后一根的收盘;
      * 最高/最低取区间极值; 成交量与成交额累加;
      * 涨跌幅按「本区间收盘 / 上一区间收盘 - 1」重新计算。
    """
    if period not in ("week", "month") or not bars:
        return list(bars)

    def bucket_key(bar: Bar) -> str:
        date_text = str(bar.date or "")
        parts = date_text.split("-")
        if len(parts) != 3 or not parts[0].isdigit():
            return date_text
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        if period == "month":
            return f"{year:04d}-{month:02d}"
        # 周线: 用 ISO 周编号分组(跨年时仍能正确区分)
        import datetime as _dt

        try:
            iso = _dt.date(year, month, day).isocalendar()
            return f"{iso[0]:04d}W{iso[1]:02d}"
        except ValueError:
            return date_text

    grouped: list[tuple[str, list[Bar]]] = []
    current_key: str | None = None
    current: list[Bar] = []
    for bar in bars:
        key = bucket_key(bar)
        if key != current_key and current:
            grouped.append((current_key or "", current))
            current = []
        current_key = key
        current.append(bar)
    if current:
        grouped.append((current_key or "", current))

    out: list[Bar] = []
    prev_close = 0.0
    for _, chunk in grouped:
        close = chunk[-1].close
        change_pct = ((close / prev_close - 1.0) * 100.0) if prev_close else 0.0
        prev_close = close or prev_close
        out.append(
            Bar(
                date=chunk[-1].date,
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=close,
                volume=sum(b.volume for b in chunk),
                amount=sum(b.amount for b in chunk),
                change_pct=round(change_pct, 3),
                turnover_rate=round(sum(b.turnover_rate for b in chunk), 3),
            )
        )
    return out


__all__ = ["KLineStore", "kline_store", "aggregate_bars", "SYNTHETIC_SOURCE"]
