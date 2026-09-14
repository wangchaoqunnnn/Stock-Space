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
    removed = user_service.remove_watchlist(codes)
    return ok({"removed": removed}, f"已移除 {removed} 只")


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
    return ok(item, "已平仓")


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
async def export_watchlist() -> Response:
    rows = user_service.list_watchlist()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["代码", "名称", "标签", "备注", "加入时间"])
    for row in rows:
        writer.writerow([row["code"], row["name"], "/".join(row["tags"]), row["note"],
                         row["added_at_text"]])
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
