# 第二阶段：本地任务执行接口

兼容扩展（2026-10-22）：网站保存任务时在附加记录中冻结默认图片 Skill 与可选风格的提示词快照。
本地 `TaskRunner` 在构造 `BackendRequest` 时使用该快照的 `compiled_prompt`，远程 `RemoteQueue` 在现有
`task.prompt` 字段传送同一文本。原始磁盘 Task 不改写，其他传输字段不变，旧执行器仍可读取；
旧任务没有快照时在执行边界应用当前默认 Skill。`work/request.json` 保存实际传给后端的提示词。
这一步不调用额外模型，不增加图片次数，不改变提交确认、积分、恢复、重试和归档行为。
`retry_archive` 不经过生图提示词整理或模型调用。详见 [图片默认 Skill](image_generation_skill.md)。

本阶段仍是可信本地脚本、单进程顺序调用，不提供网页或账号权限。
执行链固定为 `TaskRunner → ImageBackend → 本次产物 → StorageManager`。
默认 Codex 后端不调用额外付费图片 API，不自动重试生成，不修改 Codex 全局配置或登录方式。
2026-10-23 新增显式选择的 `image-api` 后端；配置、付费调用边界和验证状态见
[图片 API 接入](image_api_integration.md)。旧后端与默认值保留。

图片 API 的重试扩展（2026-10-23）：用户明确授权相同内容失败重试，Provider 在一次执行内
对明确可重试的响应及连接建立失败最多尝试 3 次；固定同一请求正文，总时限包含等待和全部尝试。
`api-request.json` 增加 `max_attempts`、`request_sha256`，`api-response.json` 增加尝试数组及次数。
不新建 Task、Execution 或积分预约，成功后结算一次，全失败时按原规则退款。鉴权/参数错误、
结果未知的传输失败、成功响应的无效产物不自动再生成；重启恢复仍只重传原结果。
并发仅启用原远程队列的执行位配置和独立 Worker 实例，不改变数据格式、归属、幂等或恢复规则。

## 接口（独立测试据此编写）

模块均放在 `image_workbench/`。以下类型也从包入口导出。

```python
@dataclass(frozen=True)
class BackendRequest:
    execution_id: str
    task: Task
    work_dir: Path
    reference_images: tuple[Path, ...]  # 本次运行的参考图副本
    edit_image: Path | None             # 本次运行的基底图副本
    stdout_path: Path
    stderr_path: Path
    timeout_seconds: float
    max_image_bytes: int

@dataclass(frozen=True)
class BackendResult:
    image_paths: tuple[Path, ...]       # 必须在本次 work/products/ 内
    returncode: int = 0

class ImageBackend(Protocol):
    name: str
    def generate(self, request: BackendRequest) -> BackendResult: ...

LocalFileBackend(source_paths: Sequence[str | Path])  # 只复制已有图片，不生成图片
CodexImageBackend(executable: str | Path | None = None,
                  generated_root: str | Path | None = None)
BackendError(message, *, status="execution_failed", returncode=None)

TaskRunner(storage: StorageManager)
runner.run(project_id, task_id, backend, *, timeout_seconds=300) -> ExecutionRecord
runner.get_execution(project_id, task_id, execution_id=None) -> ExecutionRecord | None
runner.list_executions(project_id, task_id) -> list[ExecutionRecord]
runner.retry_archive(project_id, task_id, execution_id) -> ExecutionRecord
runner.mark_interrupted(project_id, task_id, execution_id) -> ExecutionRecord
```

`get_execution(..., None)` 获取最新记录，无执行则返回 None；指定不存在的执行 ID 抛出 FileNotFoundError。
参数错误抛出 ValueError；已进入执行的后端/产物/归档错误通过持久化状态返回。元数据写入本身失败时可抛 OSError，不能声称状态已保存。
有 running/archiving 记录时拒绝再次生成；若进程被强杀，先确认旧进程已停止，再手动 mark_interrupted。
不支持并发启动多个 Python 写入进程，状态检查不是跨进程互斥锁。

`ExecutionRecord` 字段：`execution_id, project_id, task_id, backend, status, created_at,
updated_at, returncode, error, candidates, output_artifact_ids`。
`CandidateRecord` 字段：`relative_path, artifact_id, sha256, size_bytes`。
每个候选产物在归档前分配图片 ID，并把路径、哈希和 ID 写入 execution.json。
`sha256/size_bytes` 在完成验证前可为 None。全部候选验证通过后才开始登记输出。
执行记录保存相对路径，移动整个数据根目录后仍可查询或继续归档。

StorageManager.register_output_image 新增仅关键字参数 `artifact_id=None`，供执行器提交预分配的 ID。
传入的 ID 仍严格校验，已存在时抛 FileExistsError，永不覆盖；原有调用方式完全兼容。

## 状态

| 状态 | 含义 |
| --- | --- |
| running | 正在准备参考图或执行后端 |
| archiving | 后端已返回，正在验证和登记产物 |
| execution_failed | 命令启动失败、非零退出、协议错误或后端异常 |
| timed_out | 达到执行超时，已尝试终止本次 CLI 进程树；不自动重新生成 |
| missing_artifact | 返回成功但没有候选路径，或声明的产物文件不存在 |
| invalid_artifact | 候选越界、链接、不是图片、损坏、超限或归档重试时内容变化 |
| archive_failed | 已有产物，但复制收集或登记失败 |
| interrupted | 用户中断，或确认旧进程退出后手动标记中断 |
| succeeded | 全部图片验证并完成 StorageManager 登记 |

retry_archive 只处理原候选文件，绝不调用后端。succeeded 的重复归档直接返回原记录。
归档恢复会使用预分配的图片 ID 核对已登记的任务、来源和哈希，避免“登记成功但执行记录写入失败”导致重复图片。
多个输出中途归档失败时，已经登记的输出保留，恢复时只补齐其余图片，不删除历史。
候选收集尚未完成、没有完整候选清单时不能自动恢复；日志保留具体来源，可信脚本可用 LocalFileBackend 显式导入已生成文件，不需要重新生图。

## 本次执行目录

```text
projects/<project_id>/tasks/<task_id>/logs/runs/<execution_id>/
  execution.json
  stdout.log
  stderr.log
  work/
    references/             # 输入图与 edit 基底图的独立快照
    products/               # 后端明确返回的本次候选图片
    request.json            # prompt、图像角色和本次请求标识
    response.schema.json    # Codex 最终回复约定
    response.json           # 原始回复/产物来源，供诊断
    command.json            # 实际 argv（不存环境变量/登录凭据）
    process.json            # 本次 Codex 子进程 PID 和开始时间
```

初始化记录后，失败不清理本次工作目录、日志、输入快照或已产生的文件。
不通过文件修改时间“找最新图片”，不扫描电脑或整个 generated_images 目录。
正常候选限定在本次 products/ 内；目录和 JSON 路径复用 StorageManager 的越界及链接检查。

## Codex 后端约束

使用 argv 列表、shell=False、UTF-8 stdin 传入 prompt，stdout/stderr 流式写入文件。
Windows 使用实际 codex.exe，拒绝 .bat/.cmd；超时使用 taskkill /PID <本次PID> /T /F，其他系统使用独立进程组终止。
非交互参数包括 --json、--output-schema、--output-last-message、--ephemeral、--sandbox read-only、--ask-for-approval never。
使用 --no-daemon 和仅本次调用生效的 --ignore-user-config，保留现有 CODEX_HOME 登录存储；forced_login_method=chatgpt，移除子进程的 API key 环境变量，禁止 API 回退。
本次子进程启用 respect_system_proxy，沿用操作系统中既有代理，不修改全局配置。已在本机验证这能避免独立 CLI 的 WebSocket 握手超时。
本次调用关闭 shell、hooks、plugins、apps、多 Agent 和浏览器/电脑工具，以只读沙箱保护工作台源代码。
模型只调用内置图片工具一次，不自行复制文件、修改源码或重试；Python 负责确定性复制和归档。

最终 JSON 必须包含当前 execution_id、status（ok/unavailable/failed）、image_paths 和 message。
只接收该回复中明确列出的、位于配置 generated_root（默认 CODEX_HOME/generated_images）内的普通文件，拒绝链接和过期产物。
路径有效不代表图片有效，复制后仍由 TaskRunner/StorageManager 解码、检查大小及累计帧像素。
全局缓存中的原始产物只复制、不移动；完整来源保留在 response.json。改变数据目录后无需依赖缓存即可访问已归档图片。
来源绑定依赖本次结构化回复与新产物时间检查，不宣称对被攻陷的本地 Codex 进程提供独立可信证明。

## CLI

入口：`python -m image_workbench`，全局可选 `--data-root <目录>`，stdout 输出 JSON。

```text
project-create --name 名称
task-create --project PROJECT --operation generate --prompt 需求 [--input PATH ...]
task-create --project PROJECT --operation edit --prompt 修改需求 --base ARTIFACT [--input PATH ...]
run --project PROJECT --task TASK --backend local --image PATH [--image PATH ...]
run --project PROJECT --task TASK --backend codex --allow-image-generation [--timeout 300]
status --project PROJECT --task TASK [--execution RUN]
outputs --project PROJECT --task TASK
retry-archive --project PROJECT --task TASK --execution RUN
mark-interrupted --project PROJECT --task TASK --execution RUN
doctor
batch --manifest CASES.json --backend local --image PATH [--reports-root reports/local]
batch --manifest CASES.json --backend codex --allow-image-generation [--timeout 600]
```

backend 必须显式选择。codex 必须显式 --allow-image-generation，避免测试命令意外触发真实生图。
doctor 只运行本地版本/help/login status/features 检查，不调用任何模型。
退出码：0 成功/只读查询，1 执行结果失败，2 参数或无法保存状态等错误。

## 第三阶段观测扩展

ExecutionRecord 新增 failure_type、timing_events 以及 prepare/backend/codex/validate/register/total_seconds；旧记录缺字段时可读。原 status 值保持兼容。BackendResult 和 BackendError 可携带 timing_events；没有进入 Codex 阶段时 codex_seconds 为 null。

状态写入持续失败时抛出 StateUpdateError（OSError 子类），其 record 属性保留内存中的执行证据，已存在产物不清理。BatchRunner 可在独立报告中保存该证据；不能把异常当作执行成功。

批测按清单顺序、每份最多20项，无并发与自动生图重试。负例被正确拒绝也计为执行失败，但可符合预期；batch 退出码0表示所有预期及文件终检通过。详见 [批测说明](batch_testing.md)。
# 脚本直接提交与调用保护（2026-10-20）

生成按钮无需用户先报价。手选模型的预计积分来自 `/api/scripts/config`；自动报价仅作推荐、警告及独立分镜阶段预览，不调用外部需求分类模型。服务端提交时重新验证选择、当前价格与余额；价格仍以 `config/script_pricing.json` 为唯一事实来源。

免费脚本仍产生任务、幂等记录、0 积分冻结/结算记录和执行日志。新增按账号跨 Workspace 计算的保护：默认每 60 秒最多 6 次脚本提交，同时最多 3 个排队或执行任务。环境变量为 `IMAGE_WORKBENCH_SCRIPT_WINDOW_SECONDS`、`IMAGE_WORKBENCH_MAX_SCRIPT_SUBMISSIONS_PER_WINDOW`、`IMAGE_WORKBENCH_MAX_PENDING_SCRIPTS_PER_ACCOUNT`，必须为正整数。失败、退款和已完成提交仍计入时间窗口；同请求重试返回原任务，不另占次数。

容量预检避免正常拒绝留下任务，最终检查与积分冻结、outbox 在同一数据库事务内执行。余额不足、不可用模型或容量超限不调用模型。独立分镜继续按其自己的配置报价；未配置真实价格不能当作免费，明确开启的 Mock 模式除外。

## 脚本插件提交边界（2026-10-20）

`ScriptGenerationPlugin.submit` 只调用原服务保存/复用持久任务，返回原任务 DTO；
本地后台任务及远程 Worker 仍消费同一记录。插件不调用模型、不另建队列或账本。
`ScriptService.submit(..., admission=None)` 新增可选的内部准入回调，
在幂等旧请求查询之后、创建新 Task/冻结积分之前调用。原直接调用保持兼容。

注册表停用脚本插件时拒绝新生成，旧请求重试返回原任务；已受理的 queued/running/uncertain
任务继续按原规则执行、恢复和结算。停用不等于撤销已有授权，也不改变恢复为“重新生成”。
详见 [插件试点合同](architecture/PLUGIN_PILOT.md) 与 `tests/test_plugins.py`。

## H3 内置 Skill 调用（2026-10-22）

`h3-worker` 默认装配 `h3-video-workflow`，新任务在本机参数／参考图校验后、ComfyUI 提交前，使用 `gpt-6-luna` 和 Codex `service_tier="fast"` 整理单镜提示词。思考强度为独立配置。模型不负责 GPU 提交、路径、积分和归档；文本输出只能改变提示词，其余已确认字段必须保持一致。

本地 journal 新增可选 `skill_request`、`skill_plan`、`skill_error`；不改变服务端作业、HTTP 或积分协议。`skill_request` 保存完整 Skill 输入和模型配置；调用前持久化 `skill/invocation.json`，完成后沿用文本适配器的内容哈希凭据，并要求 `requested_service_tier=fast`。该字段证明请求配置，不证明服务端实际分配的处理档位。

官方指南接入后，新 `skill_request` 另外保存 `prompt_format=minimax-h3-official-v1`、官方模式 `prompt_mode` 和所用指南 `guide_sha256`，完整指南正文已经包含在冻结的 `prompt` 中。仅带此格式版本的请求要求生成正文具有顺序正确且非空的官方三段／六段字段。更新本地 Skill 或指南不改写已有请求；没有此标记的旧文本凭据仍按原约定恢复，不补跑 Luna 或用新格式追溯拒绝旧结果。发布包必须同时携带两份指南、来源记录、许可与 NOTICE。

已完成文本的重启恢复只校验原凭据，不再次调用模型。完成状态不明时仍处于 ComfyUI 提交前的 `ready`，执行器返回 `uncertain`，服务端保留已领取的运行状态和积分冻结；原凭据恢复后可继续同一任务。明确失败或输出不符合约束则按原失败流程结算，不提交视频。缺少 `skill_request` 的旧 journal 不插入调用，已提交图只对账与回收。覆盖见 `tests/test_h3_skill.py`、`tests/test_h3_worker.py`、`tests/test_script_adapter.py`。

## 脚本到分镜的独立协调与只读诊断（2026-10-22）

`ScriptStoryboardWorkflow` 负责两阶段选项/报价计划、完成后的规划触发和启动补偿。ScriptService
接收数据计划并沿原事务保存 storyboard_options/quote/confirmed，不持有或调用分镜服务。
StoryboardService 接收脚本源版本读取函数；任务事务、模型执行、候选归档仍归各服务所有。
本地 run/recover 和远程 complete 先完成脚本事务并释放锁，再由协调器触发下游。
重启扫描已完成脚本的持久意图，使用原稳定阶段 ID；只有未执行的 queued 阶段可运行。
分镜失败保持为独立失败阶段，脚本 TXT、版本和已经完成的结算不回退；重传回执不重复生成或扣款。

统一 recovery 查询仅投影状态和证据。脚本只有候选正文与哈希完成凭据一致，或原 Codex 成功回执
与正文一致时才列出恢复归档动作；部分文件或受损回执不会被当作成功证据。
图片、脚本、视频原有 uncertain/重试/人工确认规则保持，不由查询触发自动恢复或推理。


## 在线视频执行与恢复

在线视频 2.0 的执行器在提交前持久化意图，回执到达后持久化供应方任务号，重启时只查询原任务。提交结果未知时设为 uncertain 并保留冻结积分，不自动重复提交；明确拒绝或失败才释放积分。成片处理后先持久化字节及摘要，再经原视频上传和结算接口回传，回传中断时复用相同字节。旧视频和执行器默认使用 H3。在线参考图依赖可由供应方访问的域名，当前不提供；不得公开现有受保护素材来绕过限制。

在线执行器属于视频模块的 Provider 执行端，通过既有视频服务、存储和账本接口协作，不修改共享 Core。分镜新增的可选 `backend` 不要求迁移历史记录；旧记录按 H3 解释。回滚前应停止接收新的在线任务并处理已受理任务，再停用在线执行器并回退代码；保留其 journal 和原任务结果作为恢复证据。

## 在线视频的 H3 Skill 阶段

新建在线任务先由本机已登录的 Codex 文本执行器领取 planning 阶段，复用 `H3SkillPlanner`、官方指南和 gpt-6-luna fast，冻结 Skill 输入并按原凭据恢复。服务端校验计划不得改变分镜参数，保存后将同一预约交给云端 2.0 执行器；云端只使用 Skill 产出的提示词，不调用本机 H3。Skill 明确失败则释放积分；结果不明保留预约和检查点，不重复调用。已受理的旧任务不插入新阶段。

生产可设置 `IMAGE_WORKBENCH_DISABLE_H3_SUBMISSIONS=1` 禁止新提交本机 H3 任务并在页面隐藏入口，旧任务及其恢复继续按原后端处理。旧 H3 分镜需重建为在线视频分镜。planning worker 需要运行在原 Codex 登录机器；视频的供应方提交、轮询和回传在云端执行。
