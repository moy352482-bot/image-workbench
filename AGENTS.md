# 项目维护入口

## 并行开发工作区

- 当前仓库根目录以 `git rev-parse --show-toplevel` 为准。聊天历史中的绝对路径可能属于迁移前的工作区，不能据此回到原目录编辑。
- 每个开发聊天使用自己的 worktree 和分支；汇总目录为 `D:/ChatGPT/图片网站`。只在当前工作区修改代码，合并时检查源分支和目标分支。
- 开发数据、虚拟环境和网页端口分别配置；若存在 `work/worktree-info.json`，先读该文件获取本工作区信息。开发网页用 `work/start-web.ps1` 启动。
- 本机业务数据、执行器凭据及历史报告保留在原目录；测试继续使用临时数据与 Mock 后端。详见 [并行开发说明](docs/parallel_development.md)。

项目结构与模块依赖以 [docs/architecture.md](docs/architecture.md) 和
[architecture.json](architecture.json) 为准。HTTP/数据/执行行为分别参见
[Web 接口](docs/web_api_contract.md)、[存储契约](docs/storage_contract.md)、
[执行契约](docs/execution_contract.md)；这些文档不代表生产服务的实时状态。

- 完整检查：`.venv\Scripts\python.exe -X utf8 scripts\verify.py`。
- 单功能检查：加 `--module images|projects|accounts|scripts|storyboards|video|prompt|admin|storage`。
  模块检查同时运行公共接口、请求边界及前端组件检查；发布前执行完整检查。
- 新模块在 `architecture.json` 声明归属。业务模块不导入 HTTP 适配层，图片、
  脚本、视频不通过彼此的私有实现共享工具。跨功能流程使用明确服务接口。
- 全站服务由 `image_workbench/composition.py` 装配；`web.py` 保留兼容入口。
  页面安装函数接收服务，模型执行保留在已有持久队列和 Worker。
- 变更不得隐式改变 URL、磁盘格式、Workspace 权限、积分事务、提交幂等或恢复规则。
  协议改动需明确兼容策略，并更新相应契约测试。
- 测试使用独立临时数据目录与本地/Mock 后端。`data/`、`deploy/*.local.json`
  和真实执行器环境属于业务运行资源，不能作为测试夹具。

## 新功能与架构治理

新增功能前，在任务说明或实现记录中简洁回答以下问题，不要求为小改动建立额外流程文件：

1. 属于 Core、Application Service、Plugin、Skill、Workflow、Provider 还是 Frontend？
2. 依赖哪些现有模块，方向是否符合 `architecture.json`？
3. 哪些公开服务可以复用，为什么不能直接复用时才新增？
4. 是否需要新数据模型，兼容现有记录和历史版本的方式是什么？
5. 是否必须修改 Core，能否先通过应用服务/端口扩展？
6. 对其他模块、权限、积分、执行和恢复有什么实际影响？
7. 用哪些单元、服务/HTTP 集成和必要回归验证？
8. 如何按变更范围回滚，是否需要数据迁移或保留恢复证据？

优先调用既有服务：保存正式产物用 `StorageManager.register_output_*`，
先核验 Workspace；积分沿用 `IdentityStore.reserve/record_job`，
不可在插件、前端或 Skill 自行扣款。Skill 提供创作规则、Prompt 和结构约束，
确定性事务、路径、幂等和归档由 Python 服务维护。

插件只接收所需的公开服务端口，在 composition 显式注册；不得导入相邻插件私有实现、
绕过存储/账本或自行调用付费模型。停用只阻止新工作，历史、已受理任务及恢复继续由原服务维护。
跨功能 Workflow 记录阶段、源版本和依赖，通过公开接口协作；不把工作流写进共享 Core。
增加 Core 特例、动态发现或更换任务框架前，先给出实际需求、兼容方案和可验证收益。

统一检查先执行 Ruff 与架构防护，再运行测试；Ruff 历史基线不能自动刷新以接纳新问题，
不能用动态导入、入口重导出或反射规避依赖规则。细则见
[质量检查](docs/architecture/QUALITY_GATES.md)、
[插件试点](docs/architecture/PLUGIN_PILOT.md)、
[当前架构](docs/architecture/CURRENT_ARCHITECTURE.md) 与
[技术债优先级](docs/architecture/TECHNICAL_DEBT.md)。
