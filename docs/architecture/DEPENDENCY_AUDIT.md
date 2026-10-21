# 依赖审计与自动边界

2026-10-20 修改前检查得到 **79 个 Python 模块、397 个导入记录、0 个现有规则违规、0 个静态导入循环**。
导入记录按导入符号及行计数，不等同于独立模块对数。证据为
`work/architecture-governance-20261020/dependency-before.json`。
新增脚本插件后的机器报告应使用下述命令重新生成，不把文档数字当实时检查：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\check_architecture.py --json
```

## 高依赖文件

| 修改前模块 | 直接依赖它的模块数 / 分组数 | 实际影响 |
| --- | --- | --- |
| storage | 36 / 14 | Task/Artifact、路径和 JSON 工具耦合在一个文件；签名或锁语义变动影响几乎全站 |
| locking | 19 / 9 | 多进程锁、GPU 锁、恢复判定；改超时/重入需覆盖各 Worker |
| domain_errors | 17 / 10 | 共享异常到 HTTP 的映射，风险取决于异常语义，不能仅因 fan-in 高就拆分 |
| accounts | 11 / 9 | 权限、冻结、结算、outbox；必须全量回归 |
| project_manager | 7 / 6 | catalog 与归属；影响图片、文本、视频绑定 |
| task_runner | 6 / 3 | 图片执行、项目忙碌检查及查询；并非通用任务内核 |
| atomic_files | 6 / 4 | 原子替换及 Windows 重试；底层稳定工具允许高 fan-in |

上述计数来自 AST 图去重 source→target，不靠文件长度判断。
长度用于定位需深入审阅的位置：修改前 `storyboard_service.py` 901 行、`storage.py` 869 行、
`video_store.py` 787 行；`workbench.js` 1044 行、`scripts.js` 752 行。

## 静态图之外的耦合

| 位置 / 调用关系 | 结论 | 最小修复方向 |
| --- | --- | --- |
| `composition.py` 双向注入；`ScriptService._normalize/quote/_archive/_public` 调用 `storyboards`，`StoryboardService._source` 调用 `scripts.storyboard_source` | **运行时循环协作存在**，虽然 Python 导入没有环；不是递归死循环的证据。脚本归档后的分镜异常被隔离，依赖意图已持久化 | 下轮引入独立两阶段协调器和明确 source/completion 端口；保留原任务与报价合同 |
| `storage.py` 的 `_path/_check_path/_read_json/_write_json` 被多个服务调用 | 不是已证实的越权漏洞；大多数路径仍由 StorageManager 校验。但私有 API 事实上成为公共基础设施 | 建立受限 namespace 的公开元数据/路径端口，先迁移一个消费者，不一次移动全站路径 |
| `project_manager.py` → `task_runner.py`，`TaskRunner.run` → `IdentityStore.require_reservation` | Project Core 与图片执行耦合；执行器也承担授权预约校验 | 保留校验，用 `TaskActivityPort` 渐进隔离忙碌查询；不能删除冻结检查“净化分层” |
| `accounts.py:reserve` 判断 kind=script；`storage.py` 认识内容 operation | Core 含功能特例；当前规则有意允许，并非检查器漏报违规导入 | 持续新增特例前评审政策端口；现有行为不迁移、不绕过 |
| `pipeline.js:3` 解构 `window.IWVideo.state`，后续赋值 `state.project/referenceOrder` | 前端真实跨模块内部状态依赖；四个图片组件的边界规则未覆盖整个视频页面 | 先提供 getContext/updateProject/attachReferences 回调，保留事件名；之后再迁移到 ES modules |
| `ScriptService._transaction_id/submit` 查询 reservations；VideoStore 持有 `.video` 自有格式 | 应用服务认识部分持久化结构，多种结果/版本元数据并存 | 先只读接口化，避免改余额或正式媒体格式；已有视频向共享 Artifact 的桥接继续保留 |

对 `self.storage._*` 的调用点抽样：脚本服务 25、TaskRunner 22、图片状态 19、分镜服务 17；
这些仅是固定接收者 AST 调用数，不覆盖所有别名，也不证明所有访问都不安全。
本轮插件实现没有引入这些访问，仅通过 `ScriptServicePort` 调用公开服务。

## 重复与回归风险

1. 图片、文本、视频的 claim/start/heartbeat/complete 与 journal 都有相似结构，但状态和回执不同。先统一故障术语和只读投影，不能用泛型循环替代经过验证的恢复决策。
2. 原子 JSON/路径/哈希操作散布在服务中；共用工具已存在，私有 API 的公开化比创建另一套 StorageManager 更有收益。
3. 图片来源链、脚本版本索引、分镜 envelope、视频 output 元数据存在多种版本实现。统一展示模型可先行；持久化迁移必须另立兼容和回滚方案。
4. 图片固定价、脚本模型档位价、视频时长价、分镜价是不同政策，不宜机械合并。前端价格只作展示；服务端报价和 reservation 快照才约束结算。
5. Codex 图片/文本调用已共用 `codex_runtime`；结构化回复和产物收集不同，保留独立 Adapter。Comfy H3 与 Qwen 共享底层校验/锁，但工作流节点不同。

## 工具决策与防护范围

- **已用 Ruff**：固定 0.17.1，历史问题按文件/规则/消息/问题代码行及数量匹配；新增文件和变动问题行不继承豁免。见 [质量检查](QUALITY_GATES.md)。
- **保留现有 AST 契约**：明确归属、允许边、函数内/条件导入、HTTP 反向依赖、循环及前端加载顺序，已覆盖当前需求。新插件分组只能导入 `plugin_api` 和 `contracts`；直接导入 Storage/Identity/视频实现或 FastAPI 会失败，带负例测试。
- **Import Linter 技术上可用，但暂不并行引入**：它能检查当前平铺模块，不以“没有子包”为由否定。现阶段要复制分组名单，还会把已接受的 `scripts → projects → execution` 间接路径当作独立性违例；用宽泛 ignore 消音没有收益。未来分包后可从唯一配置生成契约，替换相应 AST 逻辑，而非维护两份名单。
- **暂不引入 Dependency Cruiser**：当前 JS 主要通过 window 对象和 DOM 事件关联，导入图不能捕获上述 state 共享。先解决真实状态边界，迁移 ES modules 后再启用 no-circular 和分层规则。

静态检查不能证明注入对象只调用公开方法，也不能识别任意反射、动态导入或 JS 全局运行时关系。
不允许用入口重新导出、`getattr`、动态 import 规避约束。HTTP/隔离/恢复/浏览器测试补充这些限制。
