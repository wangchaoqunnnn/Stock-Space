#!/usr/bin/env python
"""StockSpace 启动入口。

用法::

    python run.py                 # 用配置里的 host/port 启动
    python run.py --port 9000     # 临时覆盖端口
    python run.py --reload        # 开发模式(代码变更自动重启)
    python run.py --check         # 只做环境自检, 不启动服务

所有监听参数都可以用环境变量覆盖(``SS_SERVER__PORT`` 等), 不写死任何地址。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 允许直接 `python run.py` 而不用先设置 PYTHONPATH
_BACKEND_DIR = Path(__file__).resolve().parent
_PACKAGE_ROOT = _BACKEND_DIR
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))


def _banner(host: str, port: int, root_path: str) -> str:
    base = f"http://{host if host not in ('0.0.0.0', '::') else '127.0.0.1'}:{port}{root_path or ''}"
    return "\n".join([
        "",
        "  " + "=" * 60,
        "   StockSpace 股票决策平台",
        "  " + "=" * 60,
        f"   页面地址 : {base}/",
        f"   接口文档 : {base}/docs",
        f"   健康检查 : {base}/api/health",
        f"   部署自检 : {base}/selfcheck",
        "  " + "-" * 60,
        "   提示: 首次启动需要拉取全市场快照与日线缓存, 视网络约 1~5 分钟;",
        "         期间页面可用, 数据会逐步补齐。",
        "  " + "=" * 60,
        "",
    ])


def self_check() -> int:
    """不启动服务, 只验证环境与配置。"""
    from stock_space.config import config
    from stock_space.core.util import market_session
    from stock_space.paths import LOG_DIR, DATA_DIR, WEB_DIR
    from stock_space.providers import ALL_CAPABILITIES
    from stock_space.providers.endpoints import endpoints

    print("StockSpace 环境自检")
    print("-" * 60)
    print(f"Python           : {sys.version.split()[0]}")
    ok = True

    try:
        import fastapi, httpx, numpy, pandas  # noqa: F401

        print("依赖             : fastapi / httpx / numpy / pandas 均已安装")
    except ImportError as exc:
        print(f"依赖             : 缺失 -> {exc}")
        ok = False

    try:
        import psutil  # noqa: F401

        print("内存监控         : psutil 可用(精确)")
    except ImportError:
        print("内存监控         : psutil 未安装(将使用标准库兜底实现)")

    try:
        import akshare  # noqa: F401

        print("可选源 AKShare   : 已安装")
    except ImportError:
        print("可选源 AKShare   : 未安装(不影响其它数据源)")

    try:
        import Ashare  # noqa: F401

        print("可选源 Ashare    : 已安装")
    except ImportError:
        print("可选源 Ashare    : 未安装(不影响其它数据源)")

    print(f"数据目录         : {DATA_DIR}")
    print(f"日志目录         : {LOG_DIR}")
    print(f"前端目录         : {WEB_DIR} {'存在' if WEB_DIR.is_dir() else '缺失'}")
    if not (WEB_DIR / "index.html").exists():
        print("                  警告: 未找到 web/index.html, 页面将无法打开")
        ok = False

    try:
        from stock_space.providers.registry import registry

        registry.build()
        providers = registry.all()
        print(f"已注册数据源     : {', '.join(p.name for p in providers) or '(无)'}")
        missing = [p.name for p in providers if not p.installed()]
        if missing:
            print(f"未安装的源       : {', '.join(missing)}")

        print("-" * 60)
        print("各能力可用源数量:")
        problems = 0
        for capability in ALL_CAPABILITIES:
            order = registry.candidates(capability)
            mark = "OK " if order else "!! "
            if not order:
                problems += 1
            print(f"  {mark}{capability:<18} {len(order):>2} 个: {', '.join(order[:5]) or '(无)'}")
        if problems:
            print(f"\n注意: {problems} 个能力当前没有可用数据源(可能是未安装可选库或模式为 synthetic)")
    except Exception as exc:  # noqa: BLE001
        print(f"数据源注册失败   : {type(exc).__name__}: {exc}")
        ok = False

    try:
        from stock_space.store.db import db

        db.init()
        print(f"数据库           : 可用({db.path.name}, {db.stats()['rows'].get('kline_daily', 0)} 条日线)")
    except Exception as exc:  # noqa: BLE001
        print(f"数据库           : 不可用 -> {exc}")
        ok = False

    session = market_session()
    print(f"当前市场时段     : {session.label}({'需轮询' if session.should_poll else '无需轮询'}, "
          f"间隔 {session.interval_seconds}s)")
    print(f"数据源模式       : {config().source_mode}")
    print(f"上游地址目录     : {len(endpoints.all_providers())} 个源已配置地址")
    print("-" * 60)
    print("结论             : " + ("环境就绪, 可以启动服务" if ok else "存在问题, 请先按上面提示处理"))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="StockSpace 启动入口")
    parser.add_argument("--host", default=None, help="监听地址(默认取配置)")
    parser.add_argument("--port", type=int, default=None, help="监听端口(默认取配置)")
    parser.add_argument("--reload", action="store_true", help="开发模式: 代码变更自动重启")
    parser.add_argument("--workers", type=int, default=1, help="工作进程数(默认 1)")
    parser.add_argument("--check", action="store_true", help="只做环境自检")
    parser.add_argument("--log-level", default=None, help="日志级别(默认取配置)")
    args = parser.parse_args()

    if args.check:
        return self_check()

    from stock_space.config import config

    cfg = config()
    host = args.host or os.environ.get("SS_HOST") or str(cfg.get("server.host", "0.0.0.0"))
    port = args.port or int(os.environ.get("SS_PORT") or cfg.get("server.port", 8770))
    root_path = str(cfg.get("server.root_path", "") or "")
    log_level = (args.log_level or str(cfg.get("log.level", "INFO"))).lower()

    print(_banner(host, port, root_path))

    import uvicorn

    uvicorn.run(
        "stock_space.main:app" if args.reload else _app(),
        host=host,
        port=port,
        reload=args.reload,
        workers=1 if args.reload else max(1, args.workers),
        log_level=log_level,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=20,
        root_path=root_path,
    )
    return 0


def _app():
    from stock_space.main import app

    return app


if __name__ == "__main__":
    raise SystemExit(main())
