"""统一响应信封。

``{"code": 0, "message": "ok", "data": ...}`` —— 与 StockOSSecTools 的约定一致。

为什么要信封而不是裸 JSON: 前端需要在"HTTP 200 但业务失败"与"网络失败"之间
给出不同的提示(前者显示原因, 后者提示检查服务), 信封让这件事只有一个判断点。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data, "ts": time.time()}


def fail(message: str, *, code: int = 1, data: Any = None, status: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "data": data, "ts": time.time()},
    )


async def envelope_middleware(request: Request, call_next):  # noqa: ANN001, ANN201
    """把未捕获异常转成统一信封, 避免把堆栈直接抛给前端。

    静态资源与 OpenAPI 文档不做包装(否则浏览器无法解析)。
    """
    path = request.url.path
    passthrough = (
        path.startswith("/assets")
        or path.startswith("/static")
        or path in ("/", "/index.html", "/favicon.ico", "/manifest.webmanifest", "/healthz")
        or path.startswith("/docs")
        or path.startswith("/redoc")
        or path.startswith("/openapi")
    )
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        logger.exception("未捕获异常: %s %s", request.method, path)
        from ..services.market_service import ServiceUnavailable

        if isinstance(exc, ServiceUnavailable):
            return fail(
                f"数据能力 [{exc.capability}] 当前不可用：{exc.detail}。"
                "请到「数据源」页面查看各源健康度, 或手工切换到可用源。",
                code=503, status=503,
            )
        return fail(f"服务器内部错误：{type(exc).__name__}: {exc}", code=500, status=500)

    if passthrough:
        return response

    # 业务异常(API 层显式抛出的 HTTPException)也要是信封格式
    if response.status_code >= 400 and response.headers.get("content-type", "").startswith("application/json"):
        return response
    return response


def error_payload(message: str, *, code: int = 1) -> dict[str, Any]:
    return {"code": code, "message": message, "data": None, "ts": time.time()}


__all__ = ["ok", "fail", "envelope_middleware", "error_payload"]
