# 提示词强化（Prompt Enhancement）实现与验收

本功能在现有图片工作台左侧“创作需求”上增加可选的 **AI 强化**。它只做文字工作：
判断任务类型、选择创作 Skill、编译 Prompt、调用一次轻量模型并校验结果。
生成图片、积分冻结/结算、文件登记和任务状态仍由原有任务链负责。

## 架构定位

1. **归属**：Application Service（`prompt_enhancement`）加数据/规则资产（`prompt_enhancement_skills`），HTTP 适配在 `http_api/prompt_enhancements.py`，前端组件为 `web_static/workbench-prompt-enhancement.js`。
2. **依赖方向**：只依赖 `contracts`、`foundation`（共享 DeepSeek Flash 客户端）和自身模块；不导入存储、账号、图片后端或 FastAPI。依赖声明见 `architecture.json` 的 `prompt_enhancement` 组。
3. **复用**：沿用现有 DeepSeek Flash 适配器、现有图片 Style Registry（只读）和图片任务表单；“开始生成”仍是唯一生成入口。没有新增 Model Router、TaskRunner 或第二个 Style Library。
4. **数据模型**：强化请求/响应是 Pydantic 契约；任务侧只给既有 per-task options JSON 增加可选 `original_prompt`/`prompt_source`，不改 `Task` 数据类和磁盘主格式。旧任务缺少这两个键时读取为 `null`。
5. **Core 改动**：没有。任务创建仍是 `WorkbenchState.create_task` 的加法参数；强化服务本身不写任何业务数据。
6. **影响面**：不改变 URL、任务状态机、积分事务、提交幂等或恢复规则；强化失败只影响文字，不阻止用户手动继续。任务指纹包含新增的溯源字段，同请求ID不同来源会明确返回 409。
7. **验证**：单元/服务测试、45 条固定需求矩阵、前端组件测试、HTTP/请求边界回归，见下文。
8. **回滚**：删除路由注册与前端组件即可恢复原页面；已写入的 `original_prompt`/`prompt_source` 是可选字段，旧版本读取时忽略，不需要数据迁移。

## 模块与文件

| 路径 | 职责 |
| --- | --- |
| `image_workbench/prompt_enhancement.py` | 请求分类、Skill 选择、Prompt 编译、单次模型调用、结果校验、回退与幂等缓存 |
| `image_workbench/prompt_enhancement_skills.py` | 读取并校验 Skill JSON，只读适配现有 Style Registry |
| `image_workbench/contracts/prompt_enhancements.py` | 请求、编辑计划、响应、用量与来源契约 |
| `image_workbench/http_api/prompt_enhancements.py` | `POST /api/prompt-enhancements`，账号/Workspace 归属校验 |
| `image_workbench/deepseek_flash.py` | 与脚本路由共享的 DeepSeek Flash 客户端 |
| `skills/prompt-enhancement/skills/*.json` | `image-generation`、`image-editing`、`consistency`、`cinematic` 四个 Skill |
| `config/prompt_enhancement.json` | provider、路径、输入/输出上限、调用预算与可选单价 |
| `image_workbench/web_static/workbench-prompt-enhancement.js` | 本地校验、请求、原文/结果对比、回填与溯源快照 |
| `image_workbench/http_api/uploads.py` | 创建任务时接收可选的原文与来源字段 |

Skill 选择是确定性 Python 评分，不由模型决定。结果必须是统一 JSON，随后由 Pydantic 校验；
编辑任务还要求完整的改动/保留/禁止改动目标及参考图角色。

## 行为边界

- 一次强化默认最多调用一次模型；不做多轮自我反思、工具调用或自动重试。
- 不调用 `ImageBackend`，不创建任务、不执行任务、不修改任务状态。
- 不冻结或扣减积分；图片生成价格保持不变。强化没有独立积分价格。
- 不写磁盘产物或文件路径；只返回文本和元数据。
- 原文始终原样返回；回退时 `enhanced_prompt` 等于原文。
- 真实模型调用按账号+Workspace 限制每小时/每天次数；失败或预算超限同样有界回退。
- 幂等缓存键为账号+Workspace+`request_id`，冲突指纹返回 409；不同 Workspace 不共享缓存。
- 前端“使用强化版”只改输入框；不会点击或替代“开始生成”。

## 模型配置

默认 `provider` 为 `"mock"`，离线演练不产生费用。真实轻量模型使用现有脚本路由配置
`config/script_routing.json` 的 `deepseek_flash`（`enabled`、HTTPS endpoint、model、key 环境变量）。
只有同时满足以下条件才会发起真实请求：

1. `config/prompt_enhancement.json` 的 `provider` 改为 `deepseek`；
2. `deepseek_flash.enabled` 为真且 endpoint/model/key 完整；
3. 当前账号+Workspace 的预算检查通过。

尚未配置单价时，`usage.estimated_cost` 为 `null`，只记录调用次数与可用 Token；
配置 `pricing.input_per_million` / `pricing.output_per_million` 后才计算预估成本。
启用真实模型前应先由维护者确认额度与费用。

## 失败与回退码

| code | 含义 |
| --- | --- |
| `model_unavailable` | 真实模型未配置或不可用，未发起请求 |
| `budget_exceeded` | 当前账号+Workspace 达到小时/天调用上限，未发起请求 |
| `timeout` | 模型调用超时 |
| `empty_response` | 模型返回空内容 |
| `invalid_json` | 返回内容不是 JSON |
| `invalid_schema` | JSON 结构或字段不合法（含超长输出） |
| `invalid_edit_plan` | 编辑任务缺少完整的改动计划 |
| `model_error` | 其他模型/传输错误 |

这些情况都返回 `status:"fallback"`、原文和可读中文消息；任务、积分和文件不受影响。

## 任务溯源

用户点击“使用强化版”后，前端记住原文和响应中的 `source`；保存任务时附加：

- `original_prompt`：强化前的用户原文；
- `prompt_source`：`{kind:"prompt_enhancement", enhancement_id, skill_id, skill_version, mode, task_type}`。

服务端只接受上述固定字段并在任务指纹中纳入它们；任务详情返回同名字段。手动输入或恢复原文后保存时两者为空。
这满足“保留原始请求、最终提示词和任务来源”，同时不改变既有 Task 存储结构。

## 验证

```powershell
# 仅强化模块（含公共接口、边界、前端组件检查）
.\.venv\Scripts\python.exe -X utf8 scripts\verify.py --module prompt
```

离线验收矩阵 `tests/prompt_enhancement_cases.py` 固定了 45 条需求，覆盖：

- 普通文生图、创意强化、电影感、完整/模糊需求；
- 中文文字与海报版式约束、不得新增人物/道具/品牌；
- 人物一致性、产品一致性、单/多张参考图及用途；
- 局部修改的改动/保留/禁止改动目标、参考图角色、画幅继承；
- 空响应、非法 JSON、结构错误、编辑计划缺失、超时、模型不可用、模型错误。

最近一次离线结果（Mock，不消耗生图积分）：

| 指标 | 结果 |
| --- | --- |
| 固定需求数 | 45 |
| 强化成功率 | 1.0 |
| 回退处理率 | 1.0 |
| 契约拒绝率 | 1.0 |
| Schema 通过率 | 1.0 |
| 原文恢复率 | 1.0 |
| 平均 / P50 / P95 延迟 | 0.19 ms / 0.19 ms / 0.26 ms |
| 失败原因 | empty_response 1、invalid_json 1、invalid_schema 2、invalid_edit_plan 1、timeout 1、model_unavailable 1、model_error 1 |
| 用户接受率 | 未采集（离线测试，无真实用户数据） |

报告可通过 `IW_PROMPT_EVAL_REPORT=work/prompt-enhancement-eval.json` 重新生成。
延迟只反映离线 Mock，不代表真实模型耗时。

## 真实图片 A/B 验收方案（待批准）

尚未执行任何真实付费模型或真实生图请求。建议在批准后小规模执行：

1. 从矩阵中选择 6 条需求：普通文生图、模糊需求、中文海报、人物一致性、产品一致性、局部修改各 1 条。
2. 每条准备 A/B 两臂：A 为原文直接生成，B 为强化后文本生成；固定同一后端、同一比例、同一参考图，不进行人工二次改写。
3. 共 12 张图片，按当前价格预计消耗 12 次图片生成积分；强化本身若切到真实模型，另计 6 次轻量模型调用。
4. 评分维度：需求意图保留、文字准确性、主体/品牌一致性、构图可用性、是否出现未授权新增；每项 1–5 分。
5. 验收门槛：强化臂不得出现新的未授权人物/品牌/道具；意图保留平均分不低于原文臂；中文文字错误不高于原文臂。
6. 记录每条的原文、强化文本、`enhancement_id`、任务ID、图片版本和评分，再决定是否扩大样本。

执行前需明确批准真实模型调用与 12 张图片的积分消耗；未批准时只保留本离线验收。

## 回滚

1. 从 `composition.py` 移除 `install_prompt_enhancements` 注册，并移除模板中的脚本与面板；
2. 删除或停用 `config/prompt_enhancement.json`（默认 Mock，不启用真实调用）；
3. 任务中的 `original_prompt`/`prompt_source` 为可选字段，旧代码读取时忽略，无需迁移；
4. 已生成的强化报告属于测试产物，可单独清理，不影响业务数据。
