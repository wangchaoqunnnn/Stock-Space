"""服务层: 把 providers + engines + store 组合成 API 可直接调用的能力。

  * ``market_service``     —— 行情/榜单/板块/情绪/资讯聚合
  * ``user_service``       —— 自选、模拟持仓、信号流水、任务与设置
  * ``push_service``       —— 企业微信推送
  * ``scheduler_service``  —— 定时任务调度
  * ``scan_job_service``   —— 异步扫描任务(分钟级扫描不能占着 HTTP 连接)
"""

from __future__ import annotations

from .market_service import ServiceUnavailable
from .push_service import PushMessage, pusher
from .scan_job_service import ScanJob, scan_jobs
from .scheduler_service import scheduler

__all__ = [
    "ServiceUnavailable", "PushMessage", "pusher", "scheduler", "ScanJob", "scan_jobs",
]
