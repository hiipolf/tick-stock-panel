"""开盘啦 (KPH) Provider 插件.

vendor 规范: `docs/plugin-development.md` §"快速上手"
- 目录: backend/app/plugins/<your_plugin>/
- 必须: plugin.yaml + provider.py
- 可选: client.py / bridge.py / tests/

v0.2 变更: 从「扩展数据 fetcher」升级为正式 Provider。
- 端点从占位符换成真实值 (apphwshhq / apphis.kaipanhong.com, 来源 AxData 开源实现)
- 声明框架新增的 limit_board 数据集 (涨停池/跌停池/连板天梯/市场情绪/板块排行/涨停复盘)
- ⚠️ 需要先应用 framework-patch/ 才能被路由层识别
"""
__version__ = "0.2.0"
