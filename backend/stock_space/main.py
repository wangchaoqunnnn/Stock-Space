"""FastAPI 应用入口。

启动顺序(lifespan)刻意固定为: 目录 → 日志 → 数据库 → 配置 → 数据源 → 内存护栏 →
调度器。原因: 调度器依赖前四步;**内存护栏要在数据源注册缓存之后启动**,
否则初始的缓存不会被纳入治理范围。

设计取舍:
  * **前端零构建** —— 由本应用直接托管 ``stock_space/web/`` 下的静态资源,
    不需要 npm/webpack, 因此部署只有一个 Python 进程与一个端口;
  * **不做重定向魔法** —— 子路径部署通过 nginx 前缀转发完成, 应用自身
    只使用相对路径, 不依赖任何域名或绝对地址;
  * **优雅退出** —— 收到 SIGTERM 时先停调度器、关 HTTP 客户端, 再退出。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import api_router, envelope_middleware, ok
from .config import config, load_config
from .core.http import http_client
from .core.memory import memory_guard
from .core.util import now_cn
from .logging_setup import setup_logging
from .paths import WEB_DIR, ensure_dirs, relative_to_root
from .providers.endpoints import endpoints
from .providers.registry import registry
from .services import market_service, scan_jobs, scheduler
from .store.db import db
from .store.kline_store import kline_store
from .store.settings_store import settings_store

logger = logging.getLogger(__name__)

_START_TS = time.time()

#: 资源版本令牌：**每次进程启动都不同**。
#: 前端脚本没有内容哈希，仅靠 Cache-Control 不足以保证用户拿到新代码 ——
#: 实测出现过"服务端已修复、浏览器仍用旧 CSS"的情况（因为 URL 一直是 ?v=1.0.0，
#: 缓存键没变）。把启动时间戳注入资源 URL，升级重启后 URL 必然变化，
#: 浏览器一定会重新拉取。
ASSET_VERSION_TOKEN = str(int(_START_TS))


def _render_index(html: str) -> str:
    """把资源版本占位符替换成当前启动令牌。"""
    return html.replace("{{ASSET_VERSION}}", ASSET_VERSION_TOKEN)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ---------------- 启动 ----------------
    ensure_dirs()
    setup_logging(str(config().get("log.level", "INFO")))
    logger.info("=" * 68)
    logger.info("StockSpace %s 启动中…", __version__)
    logger.info("项目根目录: %s", relative_to_root(Path(__file__).resolve().parents[2]))

    db.init()
    settings_store.ensure_secure()
    endpoints.reload()

    await http_client.start()
    registry.build()
    registry.reload()

    # 内存护栏要在缓存注册之后启动
    memory_guard.configure(
        soft_limit_mb=config().memory_soft_limit_mb,
        hard_limit_mb=config().memory_hard_limit_mb,
        interval=float(config().get("quotas.memory_interval_seconds", 10) or 10),
    )
    memory_guard.start()

    if config().get("scheduler.enabled", True):
        scheduler.start()
    else:
        logger.warning("调度器已通过配置关闭(scheduler.enabled=false)")

    logger.info(
        "就绪: 数据源模式=%s 已注册源=%d 数据库=%s",
        config().source_mode, len(registry.all()), db.path.name,
    )
    logger.info("=" * 68)

    # 后台预热：首屏最慢的一步是"首次拉全市场快照"（约 8 秒，60 页）。
    # 把它放到启动后的后台任务里，用户第一次打开页面时就能命中缓存 ——
    # 否则第一位访问者要等 8~10 秒，而且多个并发请求还会重复拉取。
    prewarm_task = asyncio.create_task(_prewarm(), name="stock-space-prewarm")

    try:
        yield
    finally:
        # ---------------- 关闭 ----------------
        logger.info("正在关闭…")
        prewarm_task.cancel()
        #: 先停调度器(可能正在跑扫描), 再取消仍在执行的异步扫描任务，
        #: 否则关闭时会被未完成的任务拖住，systemd/docker 只能强杀。
        await scheduler.stop()
        await scan_jobs.shutdown()
        memory_guard.stop()
        await http_client.close()
        kline_store.cache.clear()
        db.close_thread_connection()
        logger.info("已退出, 运行时长 %.0f 秒", time.time() - _START_TS)


async def _prewarm() -> None:
    """启动后预热首屏依赖的热数据（失败不影响服务）。"""
    import asyncio as _asyncio

    await _asyncio.sleep(0.5)   # 让 lifespan 先返回，不拖慢启动
    steps = (
        ("全市场快照", lambda: registry.snapshot()),
        ("指数行情", lambda: market_service.indices()),
        ("板块列表", lambda: market_service.sectors("industry")),
        ("涨停池", lambda: market_service.limit_up_pool()),
    )
    started = time.perf_counter()
    for label, factory in steps:
        step_started = time.perf_counter()
        try:
            await factory()
            logger.info("预热 %s 完成 (%.1fs)", label, time.perf_counter() - step_started)
        except _asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 预热失败不能影响服务
            logger.warning("预热 %s 失败(不影响使用): %s", label, exc)
    logger.info("首屏预热结束, 共耗时 %.1fs", time.perf_counter() - started)


def create_app() -> FastAPI:
    cfg = config()
    docs_enabled = bool(cfg.get("server.docs_enabled", True))
    app = FastAPI(
        title=str(cfg.get("app.title") or "StockSpace"),
        description=(
            "统一股票决策平台 —— 行情多源容灾、策略选股、情绪周期、回测、"
            "复盘报告、企业微信推送、内存监控与一键部署。"
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # ---------------- 中间件 ----------------
    origins = cfg.get("server.cors_origins", []) or []
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(o) for o in origins],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        logger.info("已启用 CORS 白名单: %s", origins)

    @app.middleware("http")
    async def _envelope(request: Request, call_next):  # noqa: ANN001, ANN202
        return await envelope_middleware(request, call_next)

    @app.middleware("http")
    async def _timing(request: Request, call_next):  # noqa: ANN001, ANN202
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"
        if elapsed_ms > 3000:
            logger.warning("慢请求 %s %s 用时 %.0fms", request.method, request.url.path, elapsed_ms)
        return response

    # ---------------- 路由 ----------------
    app.include_router(api_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """极简存活探针(无信封, 供容器 HEALTHCHECK 使用)。"""
        return JSONResponse({"status": "ok", "version": __version__})

    _mount_frontend(app)
    return app


def _no_store_headers(headers: dict[str, str]) -> dict[str, str]:
    """给静态资源加上"禁止缓存"头。

    ⚠️ 这里防的是一个真实事故：前端脚本没有内容哈希，而默认的静态文件服务
    不发送任何 Cache-Control —— 浏览器会按启发式规则把 `app.js` 缓存住。
    结果是**修复了 bug 但用户仍然看到旧页面**（表现为一直卡在"正在加载"），
    排查时极易误判为服务端问题。
    """
    out = dict(headers)
    out["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    out["Pragma"] = "no-cache"
    out["Expires"] = "0"
    return out


class _NoStoreStatic:
    """包装 StaticFiles：保留其文件解析与 MIME 推断，只改写缓存头。"""

    def __init__(self, directory: Path) -> None:
        self._inner = StaticFiles(directory=str(directory))

    async def __call__(self, scope, receive, send):  # noqa: ANN001, ANN201
        async def send_wrapper(message):  # noqa: ANN001, ANN202
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                for key, value in _no_store_headers({}).items():
                    message["headers"].append((key.lower().encode("latin-1"), value.encode("latin-1")))
            await send(message)

        await self._inner(scope, receive, send_wrapper)


def _mount_frontend(app: FastAPI) -> None:
    """托管零构建前端。

    静态资源用包装后的 ``StaticFiles``（强制 no-store）; 根路径返回 ``index.html``。

    关于缓存策略的取舍：因为前端是手写的多文件脚本、没有内容哈希，
    所以**统一禁用缓存**是最安全的选择 —— 升级后刷新一定能拿到新代码。
    页面与脚本总量不到 300 KB，代价可以接受。
    """
    assets_dir = WEB_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", _NoStoreStatic(assets_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return JSONResponse(
                status_code=200,
                content={
                    "code": 0,
                    "message": "后端已就绪, 但未找到前端页面(stock_space/web/index.html)",
                    "data": {"api_docs": "/docs", "health": "/api/health"},
                },
            )
        # 读文件而不是用 FileResponse：需要把资源版本号注入 HTML
        try:
            html = index_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("读取 index.html 失败: %s", exc)
            return JSONResponse(status_code=500, content={"message": "前端页面读取失败"})
        return Response(
            content=_render_index(html),
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Any:
        icon = WEB_DIR / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return JSONResponse(status_code=204, content=None)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> Any:
        file = WEB_DIR / "manifest.webmanifest"
        if file.exists():
            return FileResponse(file, media_type="application/manifest+json")
        return JSONResponse(status_code=204, content=None)

    @app.get("/selfcheck", include_in_schema=False)
    async def selfcheck_page() -> Any:
        """部署自检页 —— 逐项检查数据源、数据库、内存、调度与推送配置。

        ⚠️ 这里必须和首页一样走 ``_render_index()`` 注入 ``{{ASSET_VERSION}}``。
        踩过的坑：原先直接用 ``FileResponse`` 返回原始文件，页面里的
        ``assets/util.js`` / ``api.js`` **没有版本号** —— 而平台其它页面都带
        ``?v=``。结果是浏览器拿旧缓存里的脚本去配新后端，用户一进自检页就报错，
        且"硬刷新"也未必修好（无版本号的 URL 没有任何缓存失效依据）。
        """
        file = WEB_DIR / "selfcheck.html"
        if not file.exists():
            return JSONResponse(status_code=404, content={"message": "自检页不存在"})
        html = _render_index(file.read_text(encoding="utf-8"))
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


app = create_app()


__all__ = ["app", "create_app"]
