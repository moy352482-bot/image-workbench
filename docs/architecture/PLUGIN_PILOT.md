# 脚本插件试点与后续实施

试点保留 `/scripts` 和全部 `/api/scripts/*` URL。只把 quote/submit 接到显式注册插件；
任务执行、恢复、下载、手动版本及 Worker 协议继续由既有服务承担。
没有动态加载、数据迁移或新的付费调用路径。

## 已实现的契约

| 接口 | 行为与边界 |
| --- | --- |
| `PluginDescriptor` | plugin_id/name/version/description/capabilities/input_schema/output_schema/dependencies/required_services；注册表的 describe/catalog 加入当前 enabled |
| `PluginContext` | account_id/workspace_id；来源为宿主已鉴权上下文，服务再次核验归属，不接受其替代鉴权 |
| `validate_input` | Pydantic 提交格式 + ScriptService 的业务与空间校验；不冻结、不执行 |
| `estimate_cost` | 使用 ScriptService.quote 与既有真实配置/模型目录；不调用外部分类模型；报价不是额度授权 |
| `submit` | 返回原始持久任务 DTO；只创建/复用任务，不等待推理；服务重算价格、校验余额、冻结并保存 outbox |
| `status` | 原服务 detail；保留 queued/running/archiving/uncertain/failed/completed 语义和空间校验 |
| `result` | `{plugin_id, task_id, status, output, failure}`；完成输出含 text/Artifact ID/Version ID/version；未完成 output=null |
| `failure` | 持久执行失败投影为 code/message/retryable=false/recoverable；输入、权限、价格冲突继续抛原异常，由 HTTP 映射；不把未知失败当可自动推理重试 |

代码：`plugin_api.py`、`script_plugin.py`、`ScriptService.validate_input/submit`。
新增的 `submit(..., admission=...)` 回调在已存在请求返回之后、创建任务和冻结之前调用；
因此旧请求重试和错误 payload 冲突语义保留。ScriptService 是受信任的应用服务，
直接内部调用须遵守业务授权；外部生成入口统一经插件，注册表不是任意 Python 代码的访问控制沙箱。

插件只持有 `ScriptServicePort` 的 getter 与注册表，不直接导入或写 Storage/Identity，
不调用 Codex Adapter，不自行改变其他模块任务状态。应用服务维护已有 Task、Credit、Artifact、Version，
也维护已有候选正文、哈希回执和原子版本提交。

## 启用与停用

默认在 `composition.create_app` 注册并启用 `script-generation`。
启动时可使用现有启动命令之前的进程环境设置：

```powershell
$env:IMAGE_WORKBENCH_DISABLED_PLUGINS = 'script-generation'
# 然后按原方式启动网站；空值或移除此变量后重启恢复默认启用。
```

也可以嵌入调用 `create_app(..., disabled_plugins=("script-generation",))`。
未知 ID 会拒绝启动，避免拼写错误导致预期的禁用未生效。
进程内测试或受信任运维可调用 `application.plugins.set_enabled("script-generation", False)`；
这是当前进程状态，不是跨进程持久开关，重启以启动配置为准。没有开放管理写接口。

停用意味着拒绝新的报价和生成提交。已受理任务继续执行/归档/结算；相同请求重试返回旧任务，
查询、下载、历史版本、手动修改、原任务恢复不受影响。已经越过 admission 的并发提交属于已受理工作，
开关不会撤销它，也不会退款或杀死执行器。`/api/scripts/config` 增加 `generation_plugin.enabled` 及描述。
现有页面不新增管理 UI，服务端返回明确的停用提示。

## 试点验收证据

`tests/test_plugins.py` 包含：

1. 仅新增一个独立 EchoPreview 测试插件即能注册；不修改图片、视频、账号实现。
2. 重复注册、缺服务、缺依赖、缺接口被拒绝；依赖启停顺序受约束。
3. 校验/报价/入队不触发 Adapter；完成由原服务产生 Task/Artifact/Version、冻结与消费记录。
4. 同请求多次提交/执行只有一次假推理、一次冻结和一次结算。
5. 停用拒绝新任务，保留 queued 任务、历史和旧请求重试；重新启用可提交。
6. 候选归档失败产生 uncertain，停用后仍可恢复且不再调用 Adapter。
7. 明确失败退款，返回结构化 failure；外部空间无法读取结果。
8. 既有 HTTP 提交入口实际使用注册表，停用响应 409，原详情/下载/权限仍生效。

测试插件仅用于证明注册契约，不会出现在生产装配；扩展数量没有被固化进 Registry。
只有本轮内置插件名单存在于 composition，新增真实插件需要登记该装配与 architecture 归属，
不需要修改无关业务模块。

## 后续可执行步骤

1. 保持此试点至少经过完整回归及一次按原流程的独立发布验收，再考虑推广。本轮没有发布或真实付费推理。
2. 选择下一个有启停/替换价值的能力，先写公开输入/异步回执/失败/恢复契约；优先在图片生成与编辑共用门面上复用原队列，禁止重建账本。
3. 为跨脚本/分镜的流程提取协调器，消除双向服务回调；不要让新插件直接调用其他插件私有方法。
4. 只有出现独立分发安装需求才加入 entry point 发现；只有出现多订阅者 lifecycle hook 才考虑 Pluggy。
5. 每步运行模块与公共合同检查；触及公共底层、权限、计费、执行恢复时运行完整验证。确定回滚清单后再发布。

## 回滚

修改前源文件在 `work/architecture-governance-20261020/source-before.zip`，带整包 SHA256 和逐文件清单。
按变更范围回退 composition/script_web/script_service 与 architecture 中的插件接入，再移除本轮两个插件模块和对应测试；
不要整包覆盖工作区。Ruff 与文档可以独立保留或回退，不依赖插件数据。
本轮没有新持久业务格式；原任务和 Artifact/Version 仍由旧服务可读。不得删除业务数据或 Worker journal 来“清理插件”。
