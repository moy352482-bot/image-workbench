# Video Script Generation — 实现与验收

2026-10-16。已实现独立的脚本／剧本 Skill、后端服务和 `/scripts` 页面，入口位于侧栏“脚本与剧本”。后续 H3 整合已加入云端文本队列和本机 Codex 执行器，`/video` 也可生成或上传 TXT；见 [H3 三步创作流程](h3_pipeline.md)。下文的初始验收记录保留当时的测试范围，线上版本以发布证据为准。

## 1. 原有模型架构

原项目的 `CodexImageBackend` 负责本机 ChatGPT 登录下的 Codex CLI 图片生成；图片 TaskRunner、图片 Worker／远端队列负责执行和归档。原图片价格由 `CreditCostCalculator` 管理。账号、Workspace、主项目、子项目、Artifact 和积分账本均已存在。

原来没有通用文本 Model Registry、Demand Router 或独立 Version 实体；图片历史依靠 `base_artifact_id` 和 `source_artifact_id` 关联。本轮复用这些身份、归属、文件存储和积分能力，只增加文本产物类型与脚本版本记录。

## 2. 新增模型配置

`config/script_models.json` 维护展示名、真实 ID、provider、启用状态及 script/screenplay 能力。当前已由本机 Codex 0.162.0-alpha.17.2 的配置和模型目录确认：

| 展示名 | model_id |
| --- | --- |
| GPT-6 Astra | `gpt-6-astra` |
| GPT-6.1 Sol | `gpt-6.1-sol` |
| GPT-6 Luna | `gpt-6-luna` |

展示名与传给 CLI 的 ID 分开。Extra High 在 CLI 中使用 `xhigh`。没有新增 API Key 登录方式，仍使用已有 Codex ChatGPT 登录。

## 3. Model Registry

`ScriptModelRegistry` 将上述配置与当前执行机的 Codex 模型目录交叉匹配，动态读取 `supported_reasoning_levels`，前端不写死思考档位。Astra/Sol 当前包含 Ultra，Luna 当前包含 Max；是否能生成还必须通过价格校验。

默认目录是当前 `CODEX_HOME`（未设置时为 `~/.codex`）下的 `models_cache.json`。目录缺失、过期、找不到执行程序或模型不在目录中时会阻止生成。目录有效期由配置决定，默认 7 天。目录可用性不等于每个账号的实时推理权益；调用失败不会切换模型，按失败处理。

可通过 `SCRIPT_CODEX_CATALOG` 指定执行机导出的目录，通过 `SCRIPT_CODEX_EXECUTABLE` 指定真实 Codex 可执行文件。不接受 `.cmd/.bat/.ps1` 包装器。新增模型需要其真实目录条目、配置项和价格；不需要修改 Skill 核心代码。

## 4. Pricing Registry

`config/script_pricing.json` 是脚本计费唯一配置来源，单位为 `one_finished_txt`。价格必须是非负整数；未配置、负数、布尔值或非整数不能变成有效报价。后端重新计算并比对报价指纹，不采信客户端传入的价格。

价格、模板或参数变更会使旧报价失效。已冻结任务按提交快照结算。免费档位仍建立零金额 freeze/consume/refund 记录；手工编辑保存属于修改文件，不调用模型，不建立生成收费预约。

## 5. 完整固定价格

单位：积分／一份 TXT。

| 模型 | Low | Medium | High | Extra High (`xhigh`) | Max | Ultra |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| GPT-6 Astra | 30 | 35 | 40 | 45 | 50 | unpriced，禁止生成 |
| GPT-6.1 Sol | 10 | 15 | 20 | 25 | 30 | unpriced，禁止生成 |
| GPT-6 Luna | 0 | 0 | 0 | 5 | unpriced，禁止生成 | 当前目录不支持 |

未列出的其他档位没有默认价格。以后目录增加新档位时，在明确配置价格前仍不可生成。

## 6. Script 与 Screenplay

`script` 面向短视频、广告、宣传、产品和品牌内容，保留主题、创意、视觉事件、旁白／对白、节奏推进和结尾。

`screenplay` 面向剧情短片、短剧和角色故事，保留分场、人物、时间地点、可拍摄动作、对白、冲突和转折。两者均为 `.txt`，正文结构由类型、模板和用户需求决定。

## 7. Template / Example Registry

独立 Skill 位于 `skills/video-script-generation/`。`registry/templates.json` 登记模板类型、名称、要求、输出结构、模板路径、案例引用、Header 策略和 enabled；可选默认模型使用 `default_model_id` 与 `default_thinking_level`。`registry/examples.json` 登记真实来源、类型、SHA256、用途和是否允许进入 Prompt。

已提供两个模板：

- `basic_video_script`：系统基础脚本结构，不冒充用户案例；`examples/scripts/` 保留为空并附说明。
- `episodic_screenplay_plaintext`：从两份真实剧本提取场次／人物／动作／对白的格式，不要求所有剧本固定三场。

真实材料按原始 UTF-8 字节导入：

| 原文件 | 用途 | SHA256 |
| --- | --- | --- |
| 我做的男朋友_第一集_审阅稿.txt | screenplay 结构／格式／风格参考 | `785b2f2289eac5aa4b362a6d3cccb5d55a668e9a977797dcdc6ea72505be47e4` |
| 合租搭子，禁止加戏_第一集_剧本正文.txt | screenplay 结构／格式／风格参考 | `0e90af42bce7608535dfd6abc3abe7f1ea4d76cedecf4382d26db0f608ba84a1` |
| 合租搭子，禁止加戏_第一集_分镜脚本(1).txt | 下游参考，`prompt_eligible=false` | `c6b980690197e2397416a807fe80314deb99881f547c4c693105c8ce6af25cc9` |

Loader 只按登记 ID 读取根目录内的 TXT，校验类型、大小、UTF-8 和案例 hash，拒绝越界路径和类型错配。案例作为引用数据，只学习结构与格式；没有明确改写要求时不复制人名、台词或具体故事。分镜文件的 9:16、4—5 分钟和 31 镜组不会成为新请求默认值。

## 8. TXT 输出和版本

输出为 UTF-8 纯文本，规范换行为 LF。成品上限 256 KiB，StorageManager 文本上限独立配置为 1 MiB；不加入 JSON、代码围栏、HTML、内部思考、系统提示或积分信息。现有剧本格式优先，不强加技术 Header。格式检查不替代对作品质量的人工判断。

物理文件沿用原存储规范：

```text
data/projects/<storage_project_id>/tasks/<task_id>/outputs/<artifact_id>.txt
```

Task、Artifact 和项目归属沿用原系统。扩展生成记录保存在 `.workbench/script-tasks/<task_id>.json`；逻辑版本保存在 `.workbench/script-versions.json`。记录模型、档位、模板与案例 hash、需求、生成 Prompt、价格、实际 freeze 流水 ID、状态、完成时间、Artifact 和版本。

每次生成修订或手工修改均新建 Task、Artifact 和 Version。Version 保存 document ID、版本号、父版本、修改原因、模型、档位、Prompt 和时间。允许从历史版本修订，版本号在同一文稿内依次增加，父版本明确记录，不覆盖旧文件。

## 9. 积分与故障恢复

提交顺序为服务端报价、余额检查、SQLite 冻结、调用模型、验证正文、登记 Artifact/Version、正式结算。余额不足不调用 Codex；明确调用失败或无效正文释放冻结积分；重复请求或重复终态不会重复扣款。

候选正文使用临时文件、fsync 和原子发布，并记录长度与 SHA256。Codex 成功返回后也保存模型、档位、Prompt hash 和正文 hash 的完成凭据。归档失败可以用已有完整正文恢复，不能重新调用模型。已有 Artifact/Version 必须与当前任务、父版本和正文匹配。截断正文、篡改凭据和越界路径不能被恢复为成功。

进程意外退出而结果不明时显示“待确认”，保持冻结，避免先退款后重复执行。页面提供“恢复 TXT 归档”；遗留 queued 任务提供“继续执行排队任务”。原进程已确认停止且没有可恢复版本时，本地操作者可沿用现有停止确认方式：

```powershell
python -m image_workbench --data-root <原数据目录> script-confirm-stopped --task <任务ID> --confirm-stopped
```

此命令不杀进程、不重跑模型。活动执行锁或已经登记版本会阻止错误退款。诊断日志写入失败不妨碍明确失败的退款。

## 10. Codex 调用链路

```text
已认证账号 / Workspace / 主项目 / 子项目
  → ScriptRequest + Template Loader + Example Loader
  → 手动模型与档位，或模板默认，或 Python DemandSpec / Router
  → Registry 校验 + Pricing + 冻结积分
  → CodexTextAdapter (--model + model_reasoning_effort)
  → 仅最终文本响应
  → TXT / Artifact / Version
  → 积分结算
```

手动模型不会被替换。高复杂度要求与低思考强度冲突时显示提示，由用户选择继续或修改。只有推荐模式按路由配置选择其他可用且已定价的模型。

推荐规则位于 `config/script_routing.json`。Python 无法可靠判断时，预留的 DeepSeek Flash 适配器只返回有严格字段限制的 DemandSpec，最终模型仍由 Python 选择。项目中没有可复用的现成 Demand Router 或 DeepSeek 配置，因此该适配器默认关闭。

启用 DeepSeek 前，应配置真实的 HTTPS chat-completions endpoint、实际模型 ID 和凭据，分别通过 `SCRIPT_DEEPSEEK_ENDPOINT`、`SCRIPT_DEEPSEEK_MODEL`、`SCRIPT_DEEPSEEK_API_KEY` 提供，再将 `deepseek_flash.enabled` 设为 true。未配置时提示补充需求或手动选择，不虚构型号或静默选模型。本轮未发送 DeepSeek 请求。

文本 Adapter 复用图片 Adapter 的 Codex 可执行文件定位、登录环境与进程清理；文本调用禁用图片生成、Shell、MCP、浏览器、插件等工具，采用只读、临时会话。它读取 `--output-last-message` 文件，不把 stdout 事件日志当正文。

## 11. 新增与修改文件

新增业务模块：`image_workbench/script_models.py`、`script_adapter.py`、`script_templates.py`、`script_service.py`、`script_web.py`。

新增界面：`image_workbench/web_templates/scripts.html`、`image_workbench/web_static/scripts.js`、`scripts.css`。

新增配置：`config/script_models.json`、`script_pricing.json`、`script_routing.json`。

新增 Skill 文件：

```text
skills/video-script-generation/
  SKILL.md
  registry/templates.json
  registry/examples.json
  templates/scripts/basic.txt
  templates/screenplays/episodic.txt
  examples/scripts/README.md
  examples/screenplays/user_robot_boyfriend_ep1.txt
  examples/screenplays/user_roommates_ep1.txt
  references/storyboard/user_roommates_ep1.txt
  references/reference-policy.md
  references/project-integration.md
```

必要集成修改：`accounts_web.py`、`cli.py`、`config.py`、`project_manager.py`、`storage.py`、`task_runner.py`、`web.py`、`workbench_state.py`、`workspace_scope.py`、`web_templates/index.html`。图片执行入口明确拒绝文本任务，图片统计不将 TXT 计入图片；脚本仍归属于同一项目体系。原账户和积分表结构、图片 Codex Adapter、视频模块及已有 Image Skill 未改变。

新增测试：`tests/test_script_adapter.py`、`test_script_models.py`、`test_script_recovery.py`、`test_script_service.py`、`test_script_storage.py`、`test_script_templates.py`、`test_script_web.py`。

文档为本文及 README 入口；本次基线备份、测试日志、截图和独立实测数据位于 `work/script-generation-v1/`，不加入真实案例 Registry。

## 12. 测试结果

- 修改前基线：315 项，312 通过、3 项环境条件跳过。
- 完整回归：400 项，396 通过、4 项环境条件跳过；其中新增 85 项包含 84 通过、1 项本机符号链接权限跳过，对应 Windows 重解析点模拟检查通过。
- JavaScript/Python 语法通过，Skill 格式校验通过。
- 浏览器实际操作通过：Astra High=40、Luna High=0、Luna Max 未定价阻止、模拟生成、余额刷新、手工修改 v2、保留 v1、TXT 下载、旧报价错误清除。没有页面控制台错误。
- 真实实测仅调用一次 `gpt-6-luna + high`：成功返回约 150 字脚本，登记 Task/Artifact/v1，freeze=0、consume=0、frozen_credit=0。没有调用图片或视频生成。

证据：[完整回归日志](../work/script-generation-v1/final-tests.log)、[真实调用结果](../work/script-generation-v1/live-smoke-result.json)、[真实 TXT](../work/script-generation-v1/live-smoke-script_bcbf091a_v1.txt)、[界面验收截图](../work/script-generation-v1/script-ui-qa.jpg)。

## 13. 本轮边界与未验证项

初始版本只验证本地文本执行。后续 H3 整合新增独立远端脚本队列：云端保存任务、积分和 TXT，本机 `image_workbench.script_remote_worker` 复用已登录的 Codex CLI 执行。模型可用性由执行器心跳提供，离线超过 120 秒显示不可用；图片、脚本和视频仍使用各自的执行协议。执行器保存持久检查点，网络重试只重传结果，执行状态不明时保留冻结并等待确认。

DeepSeek Flash 的配置入口、HTTP 适配器与结构化 Router 边界已实现，但当前没有可用真实配置，未验证真实服务调用。Astra/Sol 的价格、调用参数、隔离和错误路径通过模拟测试；本轮真实推理只验证了 Luna High。脚本真实案例仍待用户提供，没有虚构导入。

本轮未开发完整分镜、镜头规划、图片生成、视频生成或批量脚本生成。作品质量仍取决于用户需求、模板和模型输出；自动测试验证格式与系统行为，不宣称所有创作都达到某个固定质量分数。

## 14. 后续 Storyboard Skill 接口

使用 `ScriptService.storyboard_source(account_id, workspace_id, version_id)`，按明确的不可变版本读取，不能靠“最新版本”隐式漂移。返回：

```text
schema_version / generation_type
project_id / main_project_id / sub_project_id
task_id / artifact_id / version_id / version
sha256 / media_type / text
parameters（仅包含实际提供的时长、比例、受众、角色）
```

接口执行账号、空间、项目和正文 hash 校验，只提供文本输入，不创建分镜或调用下游模型。

当前 HTTP 入口：`GET /api/scripts/config`、`POST /api/scripts/quote`、`GET|POST /api/scripts/tasks`、`GET /api/scripts/tasks/{id}`、`GET .../download`、`POST .../revisions`、`POST .../run`、`POST .../recover`。未来 Storyboard 服务可在原有鉴权边界内调用上述 Python 版本读取接口，无需复制文件或改造 Script Skill。

## 15. 2026-10-20 本地创作资源增量

模板、风格、私有案例与“保存为我的模板”已经以增量方式接入以上链路。新增资源 Schema、接口、版本冻结及权限边界见 [创作资源契约](script_creative_contract.md)，本轮文件、开源研究、Mock 边界和本地验收见 [交付报告](script_template_style_engine.md)。本轮未上线，也没有真实模型调用；本页第 12 节的真实 Luna 测试属于此前版本，不是本轮执行记录。
