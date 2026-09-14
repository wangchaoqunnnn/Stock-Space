"""数据源路由: 健康度 / 手动切换 / 连通性探测 / 手工输入地址与凭据。

对应需求 8:
  * 「支持用户手动刷新数据」 → ``POST /datasources/refresh``
  * 「手动切换源」           → ``POST /datasources/{capability}/lock``
  * 「如果源需要登录，请设置后接入接口，支持手动输入」 → ``POST /datasources/{name}/credentials``
  * 「不得引用固定地址」     → ``PUT /datasources/{name}/urls`` 允许整体替换上游地址
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Query

from ...config import load_config
from ...core.http import metrics_board
from ...providers import ALL_CAPABILITIES, CAPABILITY_SPECS
from ...providers.endpoints import endpoints
from ...providers.registry import registry
from ...store.settings_store import mask_secret, settings_store
from ..response import fail, ok

logger = logging.getLogger(__name__)
router = APIRouter(tags=["datasources"])


@router.get("/datasources")
async def list_datasources() -> dict[str, Any]:
    """数据源总览: 每个源的能力、可用性、健康度、当前生效顺序。"""
    if not registry.all():
        registry.build()
    report = registry.report()
    return ok({
        **report,
        "capability_labels": {
            key: {"label": label, "method": method}
            for key, (method, label) in CAPABILITY_SPECS.items()
        },
        "all_capabilities": list(ALL_CAPABILITIES),
        "customized_endpoints": endpoints.custom_urls(),
        "note": (
            "所有上游地址集中在 config/sources.toml, 可在本页手工覆盖; "
            "健康度 = 成功率与近期滑窗的加权, 变慢或变差的源会自动沉到候选列表末尾。"
        ),
    })


@router.get("/datasources/{capability}/candidates")
async def candidates(capability: str) -> dict[str, Any]:
    if capability not in ALL_CAPABILITIES:
        return fail(f"未知能力: {capability}", code=404, status=404)
    order = registry.candidates(capability)
    return ok({
        "capability": capability,
        "label": CAPABILITY_SPECS[capability][1],
        "effective_order": order,
        "configured_order": load_config().capability_order(capability),
        "locked": registry.locks().get(capability),
    })


@router.post("/datasources/{capability}/lock")
async def lock_source(capability: str, alias: str = Body("", embed=True)) -> dict[str, Any]:
    """手动锁定某能力到指定源。``alias`` 为空表示恢复自动择优。"""
    try:
        result = registry.lock(capability, alias or None)
    except ValueError as exc:
        return fail(str(exc), code=400, status=400)
    # 落盘, 重启后依然生效
    settings_store.update({f"data_sources.locked.{capability}": alias or None})
    registry.reload()
    return ok(result, "已锁定" if alias else "已恢复自动择优")


@router.post("/datasources/unlock-all")
async def unlock_all() -> dict[str, Any]:
    registry.unlock_all()
    settings_store.update({"data_sources": {"locked": {}}})
    return ok({"locks": registry.locks()}, "已全部恢复自动择优")


@router.post("/datasources/{name}/toggle")
async def toggle_source(name: str, enabled: bool = Body(True, embed=True)) -> dict[str, Any]:
    """临时禁用/启用某个源(不参与调度, 但配置保留)。"""
    if registry.get(name) is None:
        return fail(f"未知数据源: {name}", code=404, status=404)
    registry.disable(name, disabled=not enabled)
    return ok({"name": name, "enabled": enabled, "disabled": registry.disabled()})


@router.post("/datasources/probe")
async def probe_all(capability: str = Body("quote", embed=True)) -> dict[str, Any]:
    """逐个源做真实连通性探测(会发起真实网络请求)。"""
    results = await registry.probe_all(capability)
    return ok({
        "capability": capability,
        "items": results,
        "ok_count": sum(1 for item in results if item.get("ok")),
        "total": len(results),
    })


@router.post("/datasources/{name}/probe")
async def probe_one(name: str) -> dict[str, Any]:
    provider = registry.get(name)
    if provider is None:
        return fail(f"未知数据源: {name}", code=404, status=404)
    provider.use_metric(name)
    try:
        result = await provider.probe()
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "latency_ms": 0.0, "message": f"{type(exc).__name__}: {exc}"}
    finally:
        provider.clear_metric()
    return ok({"name": name, "label": provider.label, **result})


@router.post("/datasources/refresh")
async def refresh_data(
    force: bool = Body(True, embed=True),
    capability: str = Body("snapshot", embed=True),
) -> dict[str, Any]:
    """手动刷新数据: 清空缓存 + 复位熔断器 + 重新拉取指定能力。"""
    if capability not in ALL_CAPABILITIES:
        return fail(f"未知能力: {capability}", code=404, status=404)
    cleared = registry.clear_caches()
    reset = metrics_board.reset()
    try:
        outcome = await registry.call(capability, force=True)
    except Exception as exc:  # noqa: BLE001
        return fail(f"刷新失败: {exc}", code=503, status=503)
    size = len(outcome.value) if isinstance(outcome.value, list) else 1
    return ok({
        "capability": capability,
        "source": outcome.alias,
        "attempts": outcome.attempts,
        "cleared_cache_entries": cleared,
        "reset_breakers": reset,
        "items": size,
        "force": force,
    }, f"已通过 {outcome.alias} 刷新 {size} 条数据")


@router.post("/datasources/reset-breakers")
async def reset_breakers() -> dict[str, Any]:
    """复位所有熔断器与缓存, 让被跳过的源重新参与竞争。"""
    cleared = registry.clear_caches()
    reset = registry.reset_breakers()
    return ok({"reset_breakers": reset, "cleared_cache_entries": cleared}, "已复位")


@router.get("/datasources/{name}/endpoints")
async def provider_endpoints(name: str) -> dict[str, Any]:
    """查看某个源当前实际使用的上游地址(含用户覆盖)。"""
    provider = registry.get(name)
    if provider is None:
        return fail(f"未知数据源: {name}", code=404, status=404)
    return ok({
        "name": name,
        "label": provider.label,
        "capabilities": provider.capabilities_ordered,
        "urls": {
            capability: endpoints.urls(name, capability)
            for capability in endpoints.capabilities_of(name)
        },
        "customized": endpoints.is_customized(name),
    })


@router.put("/datasources/{name}/endpoints")
async def update_endpoints(name: str, payload: dict = Body(...)) -> dict[str, Any]:
    """手工覆盖某个源的上游地址。

    请求体: ``{"capability": ["https://my-mirror/...", ...]}``,
    传空数组表示恢复 config/sources.toml 中的内置地址。
    """
    if registry.get(name) is None:
        return fail(f"未知数据源: {name}", code=404, status=404)
    cleaned: dict[str, Any] = {}
    for capability, value in payload.items():
        if capability not in ALL_CAPABILITIES:
            continue
        if isinstance(value, str):
            urls = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple)):
            urls = [str(item).strip() for item in value if str(item).strip()]
        else:
            continue
        cleaned[capability] = urls
    if not cleaned:
        return fail("没有可更新的能力(能力名或地址格式不正确)", code=400, status=400)
    settings_store.update({"data_sources": {"custom_urls": {name: cleaned}}})
    endpoints.reload()
    registry.clear_caches()
    return ok({
        "name": name,
        "updated": sorted(cleaned.keys()),
        "urls": {cap: endpoints.urls(name, cap) for cap in cleaned},
    }, "上游地址已更新")


@router.get("/datasources/{name}/credentials")
async def get_credentials(name: str) -> dict[str, Any]:
    provider = registry.get(name)
    if provider is None:
        return fail(f"未知数据源: {name}", code=404, status=404)
    raw = load_config().credentials().get(name) or {}
    masked = {
        key: mask_secret(str(value)) for key, value in (raw.items() if isinstance(raw, dict) else [])
    }
    return ok({
        "name": name,
        "requires_login": provider.requires_login,
        "configured": provider.has_credentials(),
        "fields": {key: mask_secret(str(value)) for key, value in masked.items()},
        "editable": provider.requires_login,
        "hint": (
            "请在浏览器登录目标站点后, 从开发者工具 Network 面板复制完整 Cookie 串粘贴到此处; "
            "内容会保存到服务器本地 data/runtime_settings.json(权限 600)且不会回显明文。"
            if provider.requires_login else "该数据源无需登录凭据。"
        ),
    })


@router.post("/datasources/{name}/credentials")
async def set_credentials(name: str, payload: dict = Body(...)) -> dict[str, Any]:
    """保存登录凭据(手工输入), 并立即用真实请求验证。"""
    provider = registry.get(name)
    if provider is None:
        return fail(f"未知数据源: {name}", code=404, status=404)
    cleaned = {
        str(key): str(value).strip()
        for key, value in payload.items()
        if isinstance(key, str) and value is not None
    }
    if not cleaned:
        return fail("没有提供任何凭据字段", code=400, status=400)
    settings_store.update({"data_sources": {"credentials": {name: cleaned}}})
    load_config(reload=True)
    endpoints.reload()

    provider.use_metric(name)
    try:
        result = await provider.probe()
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "message": f"{type(exc).__name__}: {exc}", "latency_ms": 0.0}
    finally:
        provider.clear_metric()
    return ok({
        "name": name,
        "saved_fields": sorted(cleaned.keys()),
        "verified": result,
    }, "凭据已保存并完成验证" if result.get("ok") else "凭据已保存, 但验证未通过(请检查 Cookie 是否过期)")


@router.delete("/datasources/{name}/credentials")
async def clear_credentials(name: str) -> dict[str, Any]:
    if registry.get(name) is None:
        return fail(f"未知数据源: {name}", code=404, status=404)
    settings_store.update({"data_sources": {"credentials": {name: {}}}})
    load_config(reload=True)
    return ok({"name": name, "configured": False}, "凭据已清除")


@router.post("/datasources/mode")
async def set_mode(mode: str = Body(..., embed=True)) -> dict[str, Any]:
    """切换数据源模式: ``auto`` / ``real`` / ``synthetic``。

    ``synthetic`` 会启用内置合成数据(仅用于离线演示与测试), 所有响应都会标注
    ``source=synthetic``, 前端会显著提示「演示数据」。
    """
    mode = str(mode or "").strip().lower()
    if mode not in ("auto", "real", "synthetic"):
        return fail("mode 必须是 auto / real / synthetic 之一", code=400, status=400)
    settings_store.update({"data_sources": {"mode": mode}})
    load_config(reload=True)
    registry.clear_caches()
    registry.clear_caches()
    return ok({
        "mode": mode,
        "synthetic_allowed": load_config().synthetic_allowed,
        "note": "synthetic 仅用于离线演示, 展示的是合成数据, 不代表真实行情。",
    }, f"数据源模式已切换为 {mode}")


__all__ = ["router"]
