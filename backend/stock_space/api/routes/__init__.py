"""API 路由汇总。

挂载点(全部相对路径, 不含任何绝对地址):

    /api/health                    存活检查(无需鉴权)
    /api/ready                     就绪检查
    /api/system/*                  系统信息 / 内存监控 / 调度 / 数据库
    /api/dashboard                 仪表盘
    /api/market/*                  时钟 / 指数 / 榜单 / 板块 / 涨停池 / 情绪
    /api/stock/*                   个股行情 / K线 / 分时
    /api/news                      资讯
    /api/review                    复盘报告
    /api/strategies/*              策略目录 / 扫描 / 评估 / 回测 / 参数
    /api/scan/jobs                 异步扫描任务(创建/轮询/取消) —— 界面走这条
    /api/datasources/*             数据源健康 / 手动切换 / 手工输入地址与凭据
    /api/settings/*                用户配置页
    /api/watchlist /api/portfolio  自选与模拟持仓
    /api/export/*                  CSV 导出
"""

from __future__ import annotations

from fastapi import APIRouter

from . import datasources, market, scan, settings, strategy, system, user

api_router = APIRouter(prefix="/api")
api_router.include_router(system.router)
api_router.include_router(market.router)
api_router.include_router(strategy.router)
api_router.include_router(scan.router)
api_router.include_router(datasources.router)
api_router.include_router(settings.router)
api_router.include_router(user.router)

__all__ = ["api_router"]
