"""用户数据路由: 自选 / 模拟持仓 / 导出。"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import Response

from ...core.util import normalize_code
from ...services import user_service
from ..response import fail, ok

logger = logging.getLogger(__name__)
router = APIRouter(tags=["user"])


# --------------------------------------------------------------------------- #
# 自选
# --------------------------------------------------------------------------- #
@router.get("/watchlist")
async def get_watchlist() -> dict[str, Any]:
    items = user_service.list_watchlist()
    return ok({"items": items, "count": len(items)})


@router.post("/watchlist")
async def add_watchlist(payload: dict = Body(...)) -> dict[str, Any]:
    try:
        code = normalize_code(payload.get("code", ""))
    except ValueError as exc:
        return fail(str(exc), code=400, status=400)
    item = user_service.add_watchlist(
        code,
        name=str(payload.get("name") or ""),
        note=str(payload.get("note") or ""),
        tags=list(payload.get("tags") or []),
    )
    return ok(item, "已加入自选")


@router.post("/watchlist/batch")
async def add_watchlist_batch(payload: dict = Body(...)) -> dict[str, Any]:
    codes = payload.get("codes") or []
    added: list[str] = []
    failed: list[str] = []
    for raw in codes:
        try:
            user_service.add_watchlist(normalize_code(str(raw)))
            added.append(str(raw))
        except ValueError:
            failed.append(str(raw))
    return ok({"added": added, "failed": failed}, f"新增 {len(added)} 只")


@router.delete("/watchlist")
async def remove_watchlist(codes: list[str] = Body(..., embed=True)) -> dict[str, Any]:
    #: 软删除 —— 保留记录与时间戳，供「历史自选股池」使用
    removed = user_service.remove_watchlist(codes)
    return ok({"removed": removed, "moved_to_history": True}, f"已移入历史自选 {removed} 只")


@router.get("/watchlist/history")
async def watchlist_history(limit: int = Query(500, ge=1, le=2000)) -> dict[str, Any]:
    """历史自选股池：已移出的标的 + 完整的放入/放出事件流水。"""
    data = user_service.watchlist_history(limit=limit)
    return ok({
        "items": data["items"],
        "events": data["events"],
        "count": len(data["items"]),
        "event_count": len(data["events"]),
    })


# --------------------------------------------------------------------------- #
# 日终快照 / 日历
# --------------------------------------------------------------------------- #
@router.get("/snapshots/dates")
async def snapshot_dates(limit: int = Query(90, ge=1, le=365)) -> dict[str, Any]:
    """哪些日期有快照 —— 前端日历据此禁用无数据的日期。"""
    from ...services import snapshot_service

    return ok({"items": snapshot_service.available_dates(limit=limit)})


@router.get("/snapshots/day")
async def snapshot_day(date: str = Query("", description="YYYY-MM-DD，留空取最新有数据的日期")) -> dict[str, Any]:
    """某一天的自选池 + 模拟持仓快照（行情中枢/情绪周期/我的持仓的日历用它）。"""
    from ...services import snapshot_service

    day = date.strip()
    if not day:
        dates = snapshot_service.available_dates(limit=1)
        day = dates[0]["trade_date"] if dates else ""
    if not day:
        return ok({"trade_date": "", "watchlist": [], "positions": [], "available": []})
    payload = snapshot_service.day_snapshot(day)
    payload["available"] = snapshot_service.available_dates(limit=90)
    return ok(payload)


@router.post("/snapshots/capture")
async def snapshot_capture(date: str = Query("", description="补跑指定日期，留空=今天")) -> dict[str, Any]:
    """手工补写一次日终快照（收盘后自动写入，这里是补跑入口）。"""
    from ...services import snapshot_service

    result = await snapshot_service.snapshot_all(date.strip())
    return ok(result, "快照已写入")


@router.put("/watchlist/{code}/note")
async def update_note(code: str, note: str = Body("", embed=True)) -> dict[str, Any]:
    try:
        normalized = normalize_code(code)
    except ValueError as exc:
        return fail(str(exc), code=400, status=400)
    if not user_service.update_watchlist_note(normalized, note):
        return fail(f"自选列表中不存在 {normalized}", code=404, status=404)
    return ok({"code": normalized, "note": note}, "备注已更新")


# --------------------------------------------------------------------------- #
# 模拟持仓
# --------------------------------------------------------------------------- #
@router.get("/portfolio")
async def get_portfolio(status: str = Query("", pattern="^(|open|closed)$")) -> dict[str, Any]:
    return ok(user_service.list_portfolio(status=status))


@router.post("/portfolio/open")
async def open_position(payload: dict = Body(...)) -> dict[str, Any]:
    try:
        item = user_service.open_position(
            payload.get("code", ""),
            float(payload.get("price") or 0),
            name=str(payload.get("name") or ""),
            shares=float(payload.get("shares") or 0),
            reason=str(payload.get("reason") or ""),
            strategy=str(payload.get("strategy") or ""),
        )
    except ValueError as exc:
        return fail(str(exc), code=400, status=400)
    return ok(item, "已加入模拟持仓")


@router.post("/portfolio/{position_id}/close")
async def close_position(position_id: int, payload: dict = Body(...)) -> dict[str, Any]:
    try:
        item = user_service.close_position(
            position_id, float(payload.get("price") or 0),
            reason=str(payload.get("reason") or ""),
        )
    except ValueError as exc:
        return fail(str(exc), code=400, status=400)
    #: 第8条：平仓**立即**结算这一笔，并带上同策略历史样本作为对照
    try:
        from ...services import performance_service

        item["review"] = performance_service.review_single_position(position_id)
    except Exception as exc:  # noqa: BLE001 - 复盘失败不应影响平仓本身
        logger.warning("平仓复盘失败 id=%s: %s", position_id, exc)
        item["review"] = {"found": False}
    return ok(item, "已平仓")


@router.get("/portfolio/performance")
async def portfolio_performance(
    strategy: str = Query(""), limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    """第7条：模拟持仓历史绩效（胜率/盈亏比 + 归因）。"""
    from ...services import performance_service

    return ok(performance_service.review_positions(strategy=strategy, limit=limit))


@router.get("/portfolio/{position_id}/review")
async def position_review(position_id: int) -> dict[str, Any]:
    """单笔持仓的复盘（含同策略历史样本对照与优化意见）。"""
    from ...services import performance_service

    data = performance_service.review_single_position(position_id)
    if not data.get("found"):
        return fail(f"持仓不存在: {position_id}", code=404, status=404)
    return ok(data)


@router.delete("/portfolio/{position_id}")
async def delete_position(position_id: int) -> dict[str, Any]:
    if not user_service.delete_position(position_id):
        return fail(f"记录不存在: {position_id}", code=404, status=404)
    return ok({"id": position_id}, "已删除")


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
@router.get("/export/signals.csv")
async def export_signals(strategy: str = "", limit: int = Query(500, ge=1, le=5000)) -> Response:
    rows = user_service.list_signals(strategy=strategy, limit=limit)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["时间", "策略", "代码", "名称", "方向", "评分", "价格", "理由"])
    for row in rows:
        writer.writerow([
            row["time"], row["strategy"], row["code"], row["name"],
            row["side"], row["score"], row["price"], row["reason"],
        ])
    # 加 BOM 让 Excel 正确识别 UTF-8
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="stock-space-signals.csv"'},
    )


@router.get("/export/watchlist.csv")
async def export_watchlist(include_removed: bool = Query(False)) -> Response:
    """导出自选。``include_removed=true`` 时连历史（已移出）一并导出。"""
    rows = user_service.list_watchlist(include_removed=include_removed)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["代码", "名称", "标签", "备注", "状态", "加入时间", "移出时间"])
    for row in rows:
        writer.writerow([row["code"], row["name"], "/".join(row["tags"]), row["note"],
                         "在池中" if row.get("status") != "removed" else "已移出",
                         row["added_at_text"], row.get("removed_at_text", "")])
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="stock-space-watchlist.csv"'},
    )


@router.post("/export/scan.csv")
async def export_scan(payload: dict = Body(...)) -> Response:
    """把一个扫描/评估结果导出为 CSV。请求体: ``{"items": [...], "filename": "..."}``"""
    items = payload.get("items") or []
    filename = str(payload.get("filename") or "stock-space-scan.csv")
    if not filename.endswith(".csv"):
        filename += ".csv"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["代码", "名称", "评分", "是否入选", "通过条件数", "总条件数",
                     "通过的条件", "未通过的条件", "止损", "止盈"])
    for item in items:
        reasons = item.get("reasons") or []
        passed = [str(r.get("name")) for r in reasons if r.get("passed")]
        failed = [str(r.get("name")) for r in reasons if not r.get("passed")]
        writer.writerow([
            item.get("code", ""), item.get("name", ""), item.get("score", ""),
            "是" if item.get("passed") else "否",
            item.get("passed_count", ""), item.get("total_count", ""),
            " / ".join(passed), " / ".join(failed),
            item.get("stop_loss", ""), item.get("take_profit", ""),
        ])
    body = "\ufeff" + buffer.getvalue()
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["router"]
