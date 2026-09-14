"""SQLite 持久层。

为什么是 SQLite: 单机自用场景下零运维、单文件备份、并发读性能足够;
所有写操作收敛到 ``asyncio.to_thread`` 的工作线程 + WAL 模式, 避免阻塞事件循环。

表设计要点:
  * ``kline_daily``  —— 日线缓存。首次全量后每个交易日只补增量, 这是"礼貌取数"的根基。
  * ``snapshot_cache`` —— 全市场快照落盘: 重启后秒级可用, 不必重新抓 5000+ 只。
  * ``scan_result``  —— 各策略扫描结果, 支撑"最近一次扫描"与历史对比。
  * ``watchlist`` / ``portfolio`` / ``signal_log`` —— 用户数据与信号流水。
  * ``news_item``    —— 资讯, 按 ``retention_days`` 定期清理。
  * ``settings_kv``  —— 任意键值配置(策略参数版本、自优化记录等)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..paths import DB_PATH, ensure_dirs

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 日线缓存: (code, date) 唯一
CREATE TABLE IF NOT EXISTS kline_daily (
    code          TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    open          REAL NOT NULL,
    high          REAL NOT NULL,
    low           REAL NOT NULL,
    close         REAL NOT NULL,
    volume        REAL NOT NULL DEFAULT 0,
    amount        REAL NOT NULL DEFAULT 0,
    change_pct    REAL NOT NULL DEFAULT 0,
    turnover_rate REAL NOT NULL DEFAULT 0,
    adj           TEXT NOT NULL DEFAULT 'qfq',
    source        TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_kline_code ON kline_daily(code);

-- 每只股票最近一次成功抓取日线的时间(判定"今日是否已抓过")
CREATE TABLE IF NOT EXISTS kline_fetch_log (
    code       TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    bars       INTEGER NOT NULL DEFAULT 0,
    last_date  TEXT NOT NULL DEFAULT ''
);

-- 全市场快照/板块/榜单等"一次性大对象"的落盘缓存
CREATE TABLE IF NOT EXISTS blob_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT ''
);

-- 策略扫描结果
CREATE TABLE IF NOT EXISTS scan_result (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL DEFAULT 0,
    rank        INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL DEFAULT '{}',
    source      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_strategy_date ON scan_result(strategy, trade_date, rank);

-- 数据源健康快照(供"数据源"页画趋势)
CREATE TABLE IF NOT EXISTS source_health (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alias       TEXT NOT NULL,
    capability  TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    latency_ms  REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_alias ON source_health(alias, created_at);

-- 自选
--   status: active=在池中 / removed=已移出（软删除，保留历史）
--   removed_at: 移出时间；重新加入会新建一行，因此历史是完整的事件流
CREATE TABLE IF NOT EXISTS watchlist (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    added_at   REAL NOT NULL,
    removed_at REAL,
    status     TEXT NOT NULL DEFAULT 'active',
    sort_order INTEGER NOT NULL DEFAULT 0
);

-- 自选进出事件流水（放入/放出各一条，含时间戳与交易日期）
--   与 watchlist 的区别：watchlist 是"当前状态"，本表是"完整历史"。
--   用户反复加入/移出同一只股票时，只有本表能还原真实过程。
CREATE TABLE IF NOT EXISTS watch_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,              -- add / remove
    price      REAL NOT NULL DEFAULT 0,    -- 事件发生时的价格（可空为 0）
    note       TEXT NOT NULL DEFAULT '',
    event_at   REAL NOT NULL,              -- 事件时间戳
    trade_date TEXT NOT NULL DEFAULT ''    -- 事件所属交易日（便于按日检索）
);
CREATE INDEX IF NOT EXISTS idx_watch_event_code ON watch_event(code, event_at);
CREATE INDEX IF NOT EXISTS idx_watch_event_date ON watch_event(trade_date);

-- 自选池每日快照（收盘后写一次）
--   日历功能按 trade_date 取"那一天的自选池长什么样"。
CREATE TABLE IF NOT EXISTS watchlist_daily (
    trade_date  TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    price       REAL NOT NULL DEFAULT 0,
    prev_close  REAL NOT NULL DEFAULT 0,
    change_pct  REAL NOT NULL DEFAULT 0,   -- 当日涨跌幅
    amount      REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT '',
    captured_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_daily_date ON watchlist_daily(trade_date);

-- 模拟持仓每日快照（收盘后写一次）
--   记录当日收盘价与**自买入起的累计涨跌幅**，用于：
--     * 日历按日查看持仓盈亏
--     * 历史胜率/盈亏比的回测与归因
CREATE TABLE IF NOT EXISTS position_daily (
    trade_date  TEXT NOT NULL,
    position_id INTEGER NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    entry_price REAL NOT NULL DEFAULT 0,   -- 建仓价（冗余存一份，便于单表回放）
    close       REAL NOT NULL DEFAULT 0,   -- 当日收盘价
    gain_pct    REAL NOT NULL DEFAULT 0,   -- 自建仓价起的累计涨跌幅 %
    gain_amount REAL NOT NULL DEFAULT 0,   -- 浮动盈亏（元）
    shares      REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    hold_days   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'open',
    source      TEXT NOT NULL DEFAULT '',
    captured_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, position_id)
);
CREATE INDEX IF NOT EXISTS idx_position_daily_date ON position_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_position_daily_code ON position_daily(code, trade_date);

-- 模拟持仓台账
CREATE TABLE IF NOT EXISTS portfolio (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    side        TEXT NOT NULL DEFAULT 'buy',
    price       REAL NOT NULL DEFAULT 0,
    shares      REAL NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    strategy    TEXT NOT NULL DEFAULT '',
    opened_at   REAL NOT NULL,
    closed_at   REAL,
    close_price REAL,
    pnl_pct     REAL,
    status      TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_portfolio_status ON portfolio(status, opened_at);

-- 信号流水
CREATE TABLE IF NOT EXISTS signal_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy   TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    side       TEXT NOT NULL DEFAULT 'watch',
    score      REAL NOT NULL DEFAULT 0,
    price      REAL NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    pushed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signal_created ON signal_log(created_at DESC);

-- 资讯
CREATE TABLE IF NOT EXISTS news_item (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL DEFAULT 'news',
    published_at  TEXT NOT NULL DEFAULT '',
    related_codes TEXT NOT NULL DEFAULT '',
    important     INTEGER NOT NULL DEFAULT 0,
    sentiment     TEXT NOT NULL DEFAULT 'neutral',
    pushed        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_created ON news_item(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_channel ON news_item(channel, created_at DESC);

-- 推送日志(去重与审计)
CREATE TABLE IF NOT EXISTS push_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    ok         INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_created ON push_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_push_fp ON push_log(fingerprint, created_at DESC);

-- 任务运行日志
CREATE TABLE IF NOT EXISTS job_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job        TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ok',
    detail     TEXT NOT NULL DEFAULT '',
    duration_ms REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_created ON job_log(created_at DESC);

-- 任意键值设置(策略参数版本、自优化记录、上次推送时间…)
CREATE TABLE IF NOT EXISTS settings_kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Database:
    """SQLite 包装: 线程局部连接 + 便捷事务 + 异步包装。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DB_PATH)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------ 连接 ------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            ensure_dirs()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path),
                timeout=20.0,
                isolation_level=None,   # 自动提交, 事务用手写 BEGIN
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=20000")
            self._local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    # ------------------------------ 初始化 ------------------------------
    def init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self.connection
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)",
                (str(time.time()),),
            )
            self._initialized = True
            logger.info("数据库就绪: %s", self.path.name)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """把已存在的旧库补齐到当前 schema。

        ``CREATE TABLE IF NOT EXISTS`` 对**已存在的表**不会新增列，因此给
        watchlist 增加 ``removed_at`` / ``status``（自选改软删除）必须显式
        ALTER。SQLite 不支持 ``ADD COLUMN IF NOT EXISTS``，只能先查
        ``PRAGMA table_info`` 再决定是否执行。
        """
        def columns(table: str) -> set[str]:
            try:
                return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                return set()

        wanted = {
            "watchlist": [
                ("removed_at", "REAL"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ],
        }
        for table, specs in wanted.items():
            existing = columns(table)
            if not existing:
                continue        # 表还不存在（本会话已由 _SCHEMA 建好则不会走到这里）
            for column, ddl in specs:
                if column in existing:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    logger.info("数据库迁移: %s 新增列 %s", table, column)
                except sqlite3.Error as exc:  # noqa: BLE001 - 迁移失败不应阻断启动
                    logger.warning("数据库迁移失败 %s.%s: %s", table, column, exc)
        #: 旧库里已存在的自选一律视为"在池中"
        try:
            conn.execute("UPDATE watchlist SET status='active' WHERE status IS NULL OR status=''")
        except sqlite3.Error:
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------ 通用 ------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self.connection.executemany(sql, seq)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def count(self, table: str) -> int:
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - 表名来自常量
        return int(row["n"]) if row else 0

    # ------------------------------ 键值 ------------------------------
    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings_kv(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings_kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def kv_all(self, prefix: str = "") -> dict[str, Any]:
        rows = self.query(
            "SELECT key, value FROM settings_kv WHERE key LIKE ? ORDER BY key",
            (f"{prefix}%",),
        )
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                out[row["key"]] = None
        return out

    def kv_delete(self, key: str) -> None:
        self.execute("DELETE FROM settings_kv WHERE key=?", (key,))

    # ------------------------------ 维护 ------------------------------
    def cleanup(self, *, news_retention_days: int = 15, job_log_keep: int = 5000) -> dict[str, int]:
        """定期清理: 资讯超期删除、日志表裁剪。返回删除条数。"""
        removed: dict[str, int] = {}
        cutoff = time.time() - max(1, news_retention_days) * 86400
        cur = self.execute("DELETE FROM news_item WHERE created_at < ?", (cutoff,))
        removed["news"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        cur = self.execute(
            "DELETE FROM job_log WHERE id NOT IN "
            "(SELECT id FROM job_log ORDER BY id DESC LIMIT ?)",
            (max(100, job_log_keep),),
        )
        removed["job_log"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        cur = self.execute(
            "DELETE FROM source_health WHERE created_at < ?", (time.time() - 7 * 86400,)
        )
        removed["source_health"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        cur = self.execute(
            "DELETE FROM scan_result WHERE trade_date < ?",
            (time.strftime("%Y-%m-%d", time.localtime(time.time() - 60 * 86400)),),
        )
        removed["scan_result"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return removed

    def vacuum(self) -> None:
        try:
            self.connection.execute("VACUUM")
        except sqlite3.Error as exc:
            logger.warning("VACUUM 失败: %s", exc)

    def stats(self) -> dict[str, Any]:
        tables = (
            "kline_daily", "kline_fetch_log", "blob_cache", "scan_result",
            "source_health", "watchlist", "portfolio", "signal_log",
            "news_item", "push_log", "job_log", "settings_kv",
        )
        counts: dict[str, int] = {}
        for table in tables:
            try:
                counts[table] = self.count(table)
            except sqlite3.Error:
                counts[table] = -1
        size_bytes = 0
        try:
            size_bytes = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            pass
        return {"path_name": self.path.name, "size_bytes": size_bytes, "rows": counts}

    # ------------------------------ 异步包装 ------------------------------
    async def run(self, fn, /, *args, **kwargs):  # noqa: ANN001, ANN201
        """把阻塞的 DB 操作丢到线程池, 避免卡住事件循环。"""
        return await asyncio.to_thread(fn, *args, **kwargs)


#: 全局单例
db = Database()


__all__ = ["Database", "db", "SCHEMA_VERSION"]
