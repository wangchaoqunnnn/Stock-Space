"""设置路由: 用户配置页(需求 3) 的全部后端能力。

安全约定:
  * 读取接口**一律掩码**敏感项(Webhook / 口令 / Cookie);
  * 写入走白名单(``EDITABLE_PATHS``), 越权路径会被拒绝并明确告知;
  * 保存后可见项立即热生效(``load_config(reload=True)``), 端口等启动项会提示需重启。
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Query, UploadFile, File
from fastapi.responses import Response

from ...config import DEFAULTS, load_config
from ...core.memory import memory_guard
from ...core.util import market_session, now_cn
from ...providers.endpoints import endpoints
from ...providers.registry import registry
from ...services import push_service, scheduler
from ...services.push_service import pusher
from ...store.settings_store import EDITABLE_PATHS, settings_store
from ..response import fail, ok

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

#: 需要重启才能生效的配置项
RESTART_REQUIRED = {"server.port", "server.host", "auth.required"}


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    """完整设置视图(敏感项已掩码)。"""
    payload = settings_store.public_snapshot()
    payload["runtime"] = {
        "session": market_session().as_dict(),
        "now": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "restart_required_paths": sorted(RESTART_REQUIRED),
        "push_stats": pusher.stats(),
        "scheduler": {
            "running": scheduler.running,
            "enabled": bool(load_config().get("scheduler.enabled", True)),
        },
    }
    payload["defaults"] = copy.deepcopy(DEFAULTS)
    return ok(payload)


@router.put("/settings")
async def update_settings(payload: dict = Body(...)) -> dict[str, Any]:
    """保存设置(白名单 + 类型校验)。"""
    result = settings_store.update(payload)
    load_config(reload=True)
    registry.reload()
    endpoints.reload()

    # 内存阈值变更需要立刻生效
    cfg = load_config()
    memory_guard.configure(
        soft_limit_mb=cfg.memory_soft_limit_mb,
        hard_limit_mb=cfg.memory_hard_limit_mb,
        interval=float(cfg.get("quotas.memory_interval_seconds", 10) or 10),
    )
    registry.clear_caches()

    touched = {p.split(".")[0] for p in result["accepted"]}
    message = f"已保存 {len(result['accepted'])} 项设置"
    if result["rejected"]:
        message += f"; 忽略 {len(result['rejected'])} 项不可写字段"
    if RESTART_REQUIRED & set(result["accepted"]):
        message += "; 端口/鉴权等启动项需重启服务后生效"
    if touched:
        message += f" (涉及: {', '.join(sorted(touched))})"
    return ok({**result, "restart_required": sorted(RESTART_REQUIRED & set(result["accepted"]))}, message)


@router.get("/settings/editable")
async def editable_paths() -> dict[str, Any]:
    return ok({
        "paths": sorted(EDITABLE_PATHS),
        "restart_required": sorted(RESTART_REQUIRED),
    })


@router.post("/settings/reset")
async def reset_settings(section: str = Body("", embed=True)) -> dict[str, Any]:
    """恢复默认设置。``section`` 为空表示全部恢复。"""
    if not section:
        settings_store.update({
            "quotas": DEFAULTS["quotas"],
            "refresh": DEFAULTS["refresh"],
            "push": DEFAULTS["push"],
            "news": DEFAULTS["news"],
            "scheduler": DEFAULTS["scheduler"],
            "data_sources": {"mode": "auto", "locked": {}, "custom_urls": {}},
        })
        message = "已恢复全部默认设置(登录凭据与自选数据保留)"
    else:
        if section not in DEFAULTS:
            return fail(f"未知分组: {section}", code=400, status=400)
        settings_store.update({section: DEFAULTS[section]})
        message = f"已恢复 [{section}] 分组的默认值"
    load_config(reload=True)
    registry.reload()
    return ok(settings_store.public_snapshot(), message)


@router.get("/settings/export")
async def export_settings(include_secrets: bool = Query(False)) -> Response:
    """导出设置。默认**不含**敏感项; ``include_secrets=true`` 时才导出明文。"""
    data = settings_store.export(include_secrets=include_secrets)
    filename = "stock-space-settings.json"
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/settings/import")
async def import_settings(payload: dict = Body(...)) -> dict[str, Any]:
    """导入设置(走同一套白名单校验)。"""
    result = settings_store.import_settings(payload)
    load_config(reload=True)
    registry.reload()
    return ok(result, f"已导入 {len(result['accepted'])} 项设置")


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #
@router.post("/settings/push/test")
async def test_push(
    webhook: str = Body("", embed=True),
    title: str = Body("", embed=True),
) -> dict[str, Any]:
    """发送一条测试推送到企业微信。

    可以临时传入 ``webhook`` 做"先测再存": 此时不写配置, 只用这一次请求验证。
    """
    if webhook:
        # 临时写入 → 测试 → 还原, 让用户能先验证再保存
        original = load_config().get("push.wecom_webhook", "")
        settings_store.update({"push": {"wecom_webhook": webhook, "enabled": True}})
        load_config(reload=True)
        try:
            message = push_service.test_message()
            if title:
                message.title = title
            result = await pusher.send(message, force=True)
        finally:
            settings_store.update({"push": {"wecom_webhook": original}})
            load_config(reload=True)
    else:
        if not pusher.webhook:
            return fail("尚未配置企业微信机器人 Webhook, 请先在设置页填写", code=400, status=400)
        message = push_service.test_message()
        if title:
            message.title = title
        result = await pusher.send(message, force=True)

    if result.ok:
        return ok(result.as_dict(), "测试推送已发送, 请查看企业微信群")
    return fail(f"测试推送失败: {result.error}", code=502, status=502, data=result.as_dict())


@router.get("/settings/push/log")
async def push_log(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return ok({"items": pusher.recent(limit), "stats": pusher.stats()})


@router.post("/settings/push/market")
async def push_market_now() -> dict[str, Any]:
    """立即推送一次市场情绪摘要(不等到定时时刻)。"""
    if not pusher.webhook:
        return fail("尚未配置企业微信机器人 Webhook", code=400, status=400)
    from ...services import market_service

    panel = await market_service.emotion_panel()
    breadth = None
    try:
        breadth = (await market_service.breadth()).as_dict()
    except Exception:  # noqa: BLE001
        pass
    message = push_service.market_message(
        emotion=panel.get("emotion") or {}, cycle=panel.get("cycle") or {},
        breadth=breadth, base_url=load_config().get("app.public_base_url", ""),
    )
    result = await pusher.send(message, force=True)
    return ok(result.as_dict(), "已推送" if result.ok else f"推送失败: {result.error}")


# --------------------------------------------------------------------------- #
# 备份 / 导入(整库)
# --------------------------------------------------------------------------- #
@router.get("/settings/backup")
async def backup() -> Response:
    """导出完整设置(含敏感项) —— 用于迁移到另一台服务器。"""
    data = settings_store.export(include_secrets=True)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="stock-space-backup.json"'},
    )


@router.post("/settings/backup/restore")
async def restore(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return fail(f"备份文件解析失败: {exc}", code=400, status=400)
    result = settings_store.import_settings(payload)
    load_config(reload=True)
    registry.reload()
    endpoints.reload()
    return ok(result, "备份已恢复")


__all__ = ["router"]
