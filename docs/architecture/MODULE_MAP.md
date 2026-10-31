# 模块职责与依赖地图

以函数调用和存储所有权核对，而非按文件名称推测。机器导入约束仍只有根 `architecture.json` 一份。
下表的层是维护职责；现有目录较平，不要求为得到这些名称而搬文件。

| 维护层 / 当前分组 | 主要实现 | 实际职责 / 公开协作面 |
| --- | --- | --- |
| Core / foundation | `models`, `config`, `locking`, `atomic_files`, `domain_errors`, `timing`, `performance`, `scheduling`, `capacity`, `registration`, `remote_access`, `codex_runtime`, `codex_observer`, `deepseek_flash`, `image_status`, `media_validation`, `video_models` | 基础记录、锁、校验和状态工具；其中 Codex 运行环境、DeepSeek Flash 客户端与视频能力是专门共享工具，不是纯领域实体 |
| Core / storage | `storage.StorageManager`、`managed_files.ManagedFiles` | Task/Artifact 登记、索引和来源链；公开文件端口维护限定目录、路径校验和原子 JSON 写；旧私有工具仅作兼容代理 |
| Core / identity | `accounts.IdentityStore` | 身份、空间、SQLite 钱包与 reservation/outbox；积分不是各插件自行记账 |
| Core + Application / projects | `project_manager`, `workspace_scope`, `account_migration` | catalog、迁移、逻辑归属和权限门面；忙碌判断仍依赖图片 TaskRunner |
| Provider / image_backend | `image_backend`, `codex_backend` | ImageBackend Protocol；LocalFileBackend 离线复制；CodexImageBackend 一次调用和产物采集 |
| Application / execution | `task_runner` | 图片执行、候选验证、归档恢复、耗时；拒绝文本 operation，非全局通用执行引擎 |
| Application / images | `workbench_state`, `image_queries`, `remote_queue`, `worker`, `remote_worker`, `batch_runner` | 图片任务提交/查询、持久队列、远程协议及离线批测；读写职责已部分分离 |
| Application + Provider / scripts | `script_service`, `script_models`, `script_adapter`, `script_remote`, `script_remote_worker` | 脚本校验/报价/提交/恢复/版本，实际模型目录、文本 Adapter 和远程任务；运行状态由此维护 |
| Skill 宿主 / scripts | `script_templates`, `script_resources`, `script_engine` | 模板/风格/案例解析与版本快照、规则分析、Prompt 构建、结构报告 |
| Plugin 契约 / plugin_api | `plugin_api` | 描述、Context、Protocol、显式注册及启停；不接触账号数据库和产物目录 |
| Plugin / script_plugin | `script_plugin` | 把通用扩展接口映射到 ScriptServicePort；只允许导入插件契约和请求 schema |
| Application / storyboards | `storyboard_service`, `storyboard_models`, `storyboard_planner` | 独立规划、人工审核、局部重规划、版本、视频镜头绑定；通过源版本读取端口取得脚本 |
| Workflow / workflows | `script_workflow` | 两阶段报价计划、脚本完成后的分镜触发、源版本和失败阶段关联、启动时补齐持久意图 |
| Read model / foundation | `task_recovery` | 图片、脚本和视频的统一诊断响应；保留原生状态，不执行恢复或结算 |
| Application + Provider / video | `video_store`, `h3_worker` | 视频项目/镜头/自有存储、报价、队列、ComfyUI 提交/对账与媒体验证 |
| Application + Skill 数据 / prompt | `prompt_enhancement`, `prompt_enhancement_skills` | 请求分类、Skill/Style 选择、Prompt 编译、单次文本强化、JSON 校验与有界回退；不生成图片、不记账、不写文件 |
| Workflow / pipeline | `pipeline_service` | 图片、脚本和视频的绑定与引用；通过 `PipelineVideoPort` 和注入服务协作 |
| Operations | `admin_operations` | 检查、发布包和运行信息；不同于业务核心 |
| Presentation / contracts | `contracts/*` | HTTP 请求 Pydantic schema；现在脚本插件也复用该输入格式，仍不依赖业务服务 |
| Presentation / http | `http_api/*`, `script_web`, `script_resource_web`, `storyboard_web`, `video_web`, `video_pipeline`, `admin_web` | 请求解析、鉴权上下文、错误、下载、后台调度；具体服务由装配注入 |
| Presentation / accounts_http | `accounts_web` | 登录、会话、工作空间应用隔离；创建 scoped 图片应用 |
| Composition / entrypoints | `composition`, `web`, `cli`, `__main__`, `__init__` | 装配与兼容导出；`web` 保留旧入口，不把业务重新塞进入口 |
| Frontend | `web_static/*.js`, `web_templates/*.html` | 原生页面、工厂和事件；视频消费者使用副本及受上下文约束的命令，不能直接共享可变 state |

## 新需求的适合归属

- **适合下一步插件化**：存在输入/异步提交/状态/产物边界且需启停或替换的脚本、图片生成/编辑能力、分镜规划。图片生成与编辑可共用一个能力插件，不必机械拆分。
- **有实际需求后再做**：视频生成/拼接、连续性审阅、Audio/TTS、字幕。先有服务和稳定数据合同，再建立插件描述；不要只登记一个不能执行的空插件。
- **继续简单维护**：Account、Workspace、Project、Credit、Storage、锁、哈希、Artifact 基础模型、页面格式工具。它们是数据所有者或小工具，不通过动态 hook 扇出执行。
- **保留为数据/规范**：模板、风格、案例与 Skill。修改模板不应要求创建一个 Python 插件；确定性落盘和积分不交给 Skill。
- **保留为工作流**：脚本→分镜→视频等流程调用服务/插件，记录源版本及阶段状态，不直接写相邻模块数据库。

## 需要复用的接口

保存图片/文本/文档使用 `StorageManager.register_output_image/register_output_text/register_output_document`；
先通过 `WorkspaceProjects` 或对应服务校验归属。需要积分用 `IdentityStore.reserve/record_job` 的既有事务路径。
通用插件只拿完成任务所需的应用服务端口，不直接拿整个 application、IdentityStore 或文件路径。
配置和模型目录决定实际可用能力，插件静态 capabilities 不代表当前执行机一定在线或已经真实推理验收。
