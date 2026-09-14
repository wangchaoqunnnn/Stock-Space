"""StockSpace 后端包。

统一股票决策平台: 把 11 个独立股票项目整合为一个可部署的单一服务。

设计约束(贯穿全仓):
  1. 代码中不得出现写死的绝对地址/固定地址 —— 第三方上游地址集中在
     ``stock_space.providers.endpoints``(可被 ``config/sources.toml`` 与环境变量覆盖),
     运行期路径全部由 ``stock_space.paths`` 相对推导。
  2. 前端零构建、零 CDN、零外部依赖, 一切请求使用相对路径。
  3. 对外不可用时宁可明确报错, 也不把合成数据当真实数据展示
     (合成数据必须由用户显式开启, 且响应中带 ``synthetic: true``)。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
