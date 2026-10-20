# 项目维护入口

项目结构与模块依赖以 [docs/architecture.md](docs/architecture.md) 和
[architecture.json](architecture.json) 为准。HTTP/数据/执行行为分别参见
[Web 接口](docs/web_api_contract.md)、[存储契约](docs/storage_contract.md)、
[执行契约](docs/execution_contract.md)；这些文档不代表生产服务的实时状态。

- 完整检查：`.venv\Scripts\python.exe -X utf8 scripts\verify.py`。
- 单功能检查：加 `--module images|projects|accounts|scripts|storyboards|video|qwen|admin|storage`。
  模块检查同时运行公共接口、请求边界及前端组件检查；发布前执行完整检查。
- 新模块在 `architecture.json` 声明归属。业务模块不导入 HTTP 适配层，图片、
  脚本、视频、Qwen 不通过彼此的私有实现共享工具。跨功能流程使用明确服务接口。
- 全站服务由 `image_workbench/composition.py` 装配；`web.py` 保留兼容入口。
  页面安装函数接收服务，模型执行保留在已有持久队列和 Worker。
- 变更不得隐式改变 URL、磁盘格式、Workspace 权限、积分事务、提交幂等或恢复规则。
  协议改动需明确兼容策略，并更新相应契约测试。
- 测试使用独立临时数据目录与本地/Mock 后端。`data/`、`deploy/*.local.json`
  和真实执行器环境属于业务运行资源，不能作为测试夹具。
