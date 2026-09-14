"""用户数据服务: 自选、模拟持仓台账、信号流水、任务与设置汇总。

"模拟持仓"是刻意做的: 各原始项目都有"纸面交易/观察池"的设计,
它能让人在不承担真实风险的情况下验证策略 —— 同时平台**不做任何自动交易**。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Sequence

from ..config import config
from ..core.util import normalize_code, now_cn, today_str
from ..store.db import db
from ..store.settings_store import settings_store

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 自选
# --------------------------------------------------------------------------- #
def list_watchlist() -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM watchlist ORDER BY sort_order, added_at DESC")
    return [
        {
            "code": row["code"], "name": row["name"], "note": row["note"],
            "tags": [t for t in str(row["tags"] or "").split(",") if t],
            "added_at": row["added_at"],
            "added_at_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["added_at"])),
            "sort_order": row["sort_order"],
        }
        for row in rows
    ]


def add_watchlist(code: str, name: str = "", note: str = "", tags: Sequence[str] = ()) -> dict[str, Any]:
    code = normalize_code(code)
    now = time.time()
    db.execute(
        "INSERT INTO watchlist(code, name, note, tags, added_at, sort_order) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, note=excluded.note, tags=excluded.tags",
        (code, name, note, ",".join(tags), now, 0),
    )
    return {"code": code, "name": name, "note": note, "tags": list(tags), "added_at": now}


def remove_watchlist(codes: Sequence[str]) -> int:
    if not codes:
        return 0
    targets = [normalize_code(c) for c in codes]
    placeholders = ",".join("?" for _ in targets)
    cur = db.execute(f"DELETE FROM watchlist WHERE code IN ({placeholders})", tuple(targets))
    return cur.rowcount or 0


def update_watchlist_note(code: str, note: str) -> bool:
    code = normalize_code(code)
    cur = db.execute("UPDATE watchlist SET note=? WHERE code=?", (note, code))
    return bool(cur.rowcount)


# --------------------------------------------------------------------------- #
# 模拟持仓
# --------------------------------------------------------------------------- #
def list_portfolio(*, status: str = "") -> dict[str, Any]:
    if status:
        rows = db.query(
            "SELECT * FROM portfolio WHERE status=? ORDER BY opened_at DESC", (status,)
        )
    else:
        rows = db.query("SELECT * FROM portfolio ORDER BY opened_at DESC")
    items = [_portfolio_row(row) for row in rows]
    open_items = [item for item in items if item["status"] == "open"]
    closed = [item for item in items if item["status"] == "closed"]
    wins = [item for item in closed if (item["pnl_pct"] or 0) > 0]
    return {
        "items": items,
        "open_count": len(open_items),
        "closed_count": len(closed),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "avg_pnl_pct": round(
            sum(item["pnl_pct"] or 0 for item in closed) / len(closed), 3
        ) if closed else 0.0,
        "total_pnl_pct": round(sum(item["pnl_pct"] or 0 for item in closed), 3) if closed else 0.0,
    }


def _portfolio_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "code": row["code"], "name": row["name"], "side": row["side"],
        "price": row["price"], "shares": row["shares"], "reason": row["reason"],
        "strategy": row["strategy"], "status": row["status"],
        "opened_at": row["opened_at"],
        "opened_at_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["opened_at"])),
        "closed_at": row["closed_at"],
        "closed_at_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["closed_at"]))
        if row["closed_at"] else "",
        "close_price": row["close_price"], "pnl_pct": row["pnl_pct"],
    }


def open_position(
    code: str, price: float, *, name: str = "", shares: float = 0.0,
    reason: str = "", strategy: str = "",
) -> dict[str, Any]:
    code = normalize_code(code)
    if price <= 0:
        raise ValueError("开仓价必须大于 0")
    now = time.time()
    cur = db.execute(
        "INSERT INTO portfolio(code, name, side, price, shares, reason, strategy, opened_at, status) "
        "VALUES(?,?,?,?,?,?,?,?, 'open')",
        (code, name, "buy", float(price), float(shares), reason, strategy, now),
    )
    return {"id": cur.lastrowid, "code": code, "price": float(price), "opened_at": now}


def close_position(position_id: int, price: float, *, reason: str = "") -> dict[str, Any]:
    row = db.query_one("SELECT * FROM portfolio WHERE id=?", (position_id,))
    if row is None:
        raise ValueError(f"持仓不存在: {position_id}")
    if price <= 0:
        raise ValueError("平仓价必须大于 0")
    entry = float(row["price"]) or 0.0
    pnl_pct = ((float(price) - entry) / entry * 100.0) if entry else 0.0
    now = time.time()
    db.execute(
        "UPDATE portfolio SET status='closed', closed_at=?, close_price=?, pnl_pct=?, "
        "reason=CASE WHEN ?='' THEN reason ELSE reason || ' | 离场: ' || ? END WHERE id=?",
        (now, float(price), pnl_pct, reason, reason, position_id),
    )
    return {"id": position_id, "close_price": float(price), "pnl_pct": round(pnl_pct, 3),
            "closed_at": now}


def delete_position(position_id: int) -> bool:
    cur = db.execute("DELETE FROM portfolio WHERE id=?", (position_id,))
    return bool(cur.rowcount)


# --------------------------------------------------------------------------- #
# 信号流水
# --------------------------------------------------------------------------- #
def log_signals(signals: Sequence[Any], *, strategy: str = "", side: str = "watch") -> int:
    """把扫描出的信号写入流水(按 code+strategy+当天 去重)。"""
    rows = []
    now = time.time()
    day = today_str()
    for signal in signals:
        payload = signal.as_dict() if hasattr(signal, "as_dict") else dict(signal)
        rows.append((
            strategy or payload.get("strategy", ""), payload.get("code", ""),
            payload.get("name", ""), side, float(payload.get("score", 0) or 0),
            float((payload.get("metrics") or {}).get("close", 0) or 0),
            "；".join(
                str(r.get("name")) for r in (payload.get("reasons") or []) if r.get("passed")
            )[:500],
            _json(payload), now,
        ))
    if not rows:
        return 0
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM signal_log WHERE strategy=? AND date(created_at,'unixepoch','+8 hours')=?",
            (strategy, day),
        )
        conn.executemany(
            "INSERT INTO signal_log(strategy, code, name, side, score, price, reason, payload, "
            "created_at, pushed) VALUES(?,?,?,?,?,?,?,?,?,0)",
            rows,
        )
    return len(rows)


def list_signals(*, strategy: str = "", limit: int = 100) -> list[dict[str, Any]]:
    if strategy:
        rows = db.query(
            "SELECT * FROM signal_log WHERE strategy=? ORDER BY created_at DESC LIMIT ?",
            (strategy, max(1, min(500, limit))),
        )
    else:
        rows = db.query(
            "SELECT * FROM signal_log ORDER BY created_at DESC LIMIT ?",
            (max(1, min(500, limit)),),
        )
    return [
        {
            "strategy": row["strategy"], "code": row["code"], "name": row["name"],
            "side": row["side"], "score": row["score"], "price": row["price"],
            "reason": row["reason"], "pushed": bool(row["pushed"]),
            "created_at": row["created_at"],
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"])),
        }
        for row in rows
    ]


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# 系统状态汇总
# --------------------------------------------------------------------------- #
def system_overview(*, memory_report: Mapping[str, Any], scheduler_report: Mapping[str, Any]) -> dict[str, Any]:
    from ..providers.registry import registry
    from ..store.kline_store import kline_store

    try:
        db_stats = db.stats()
    except Exception as exc:  # noqa: BLE001
        db_stats = {"error": str(exc)}

    return {
        "app": {
            "title": config().get("app.title"),
            "version": _version(),
            "timezone": config().get("app.timezone"),
            "now": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": today_str(),
            "source_mode": config().source_mode,
            "disclaimer": config().get("app.disclaimer"),
        },
        "settings": settings_store.info(),
        "memory": memory_report,
        "scheduler": scheduler_report,
        "database": db_stats,
        "kline": kline_store.stats(),
        "sources": {
            "providers": len(registry.all()),
            "locks": registry.locks(),
            "disabled": registry.disabled(),
        },
    }


def _version() -> str:
    from .. import __version__

    return __version__


# --------------------------------------------------------------------------- #
# 原始项目对照(需求文档要求"可追溯到来源")
# --------------------------------------------------------------------------- #
INTEGRATION_MAP: list[dict[str, Any]] = [
    {
        "project": "92KeBi",
        "contribution": (
            "情绪周期状态机 + 龙头四分层 + 仓位建议 + 企微多 webhook 推送；"
            "**并提供了 money_flow 能力的唯一非东财源**（新浪日度资金流）"
        ),
        "landed_in": [
            "engines/emotion.py", "services/push_service.py", "api/routes/market.py",
            "providers/sina.py", "config/sources.toml",
        ],
    },
    {
        "project": "FinancialNews", "contribution": "财经资讯聚合(新浪7×24/巨潮公告/传闻) + 重要消息推送去重与频控",
        "landed_in": ["services/market_service.py", "providers/social.py", "services/push_service.py"],
    },
    {
        "project": "Limit-Up-Pullback-Buy-Setup", "contribution": "涨停回调低吸五信号 + 多源故障转移 + 内存护栏 + Docker 多阶段构建",
        "landed_in": ["engines/limit_up_pullback.py", "providers/registry.py", "core/memory.py", "deploy/Dockerfile"],
    },
    {
        "project": "MarketAlerts", "contribution": "异动/涨速预警阈值 + 自选股炸板提醒",
        "landed_in": ["engines/emotion.py", "services/scheduler_service.py"],
    },
    {
        "project": "NPatternStrategy", "contribution": "N字战法量化判定 + 大盘环境闸门 + 参数版本化 + 部署脚本范式",
        "landed_in": ["engines/n_pattern.py", "store/settings_store.py", "deploy/deploy.sh"],
    },
    {
        "project": "QuietRiseScanner", "contribution": "潜涨五维加权评分 + 板块共振加分 + 内存监控面板 + nginx/systemd 模板",
        "landed_in": ["engines/quiet_rise.py", "core/memory.py", "deploy/nginx.conf", "deploy/stock-space.service"],
    },
    {
        "project": "ReviewNotification", "contribution": "复盘报告结构化模块 + 企微 markdown 重试幂等 + 15 天保留清理 + 响应式报告样式",
        "landed_in": ["engines/review.py", "services/push_service.py", "services/market_service.py", "web/assets/app.css"],
    },
    {
        "project": "stock-pattern-discovery", "contribution": "16 套经典形态库 + ATR 止损/移动止盈模板 + 保守成本口径 + 自检页设计",
        "landed_in": ["engines/pattern.py", "engines/backtest.py", "web/index.html"],
    },
    {
        "project": "StockOSSecTools", "contribution": "指数多市场卡片 + 技术指标计算 + 子路径部署(相对地址) + 统一响应信封",
        "landed_in": ["api/response.py", "engines/indicators.py", "web/assets/app.js", "deploy/nginx.conf"],
    },
    {
        "project": "StockTradingReviewTool", "contribution": "加权情绪分 0~100 + 五阶段周期判定 + 大面/晋级率口径",
        "landed_in": ["engines/emotion.py"],
    },
    {
        "project": "TrendSniper", "contribution": "趋势票十维加权打分 + 止损三选一 + 每周参数自调优思路 + 零依赖 Canvas 前端",
        "landed_in": ["engines/trend.py", "engines/backtest.py", "web/assets/charts.js"],
    },
]


__all__ = [
    "list_watchlist", "add_watchlist", "remove_watchlist", "update_watchlist_note",
    "list_portfolio", "open_position", "close_position", "delete_position",
    "log_signals", "list_signals", "system_overview", "INTEGRATION_MAP",
]
