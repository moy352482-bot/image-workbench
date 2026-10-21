# 阶段 C 开发前审计

审计日期：2026-10-20（Asia/Shanghai）。

审计基线：`D:\ChatGPT\图片网站\work\asset-ux-20261020\source`，本地提交 `310f7d8`（Local baseline before asset UX changes）。最终结论均以该独立目录为准；原工作区存在其他聊天的同时修改，不作为本报告可复现基线。审计仅写本报告，未修改业务代码，未访问生产，未调用任何真实模型。

## 结论

现有脚本价格已完整符合用户的 14 项产品定价，无需修改 `config/script_pricing.json`。价格、额度冻结、完成扣款、失败退款、幂等和版本归档已有可靠实现，应继续复用。阶段 C 的主要工作是两个脚本入口统一为直接展示积分并开始生成、移除提交必须预先取得报价签名的限制，以及补足免费脚本的默认次数限流和并发限制。

独立源目录还保留了一处复制时已有的接口不一致：`scripts.js` 提交新的风格、案例与变量字段，后端合同尚不接受。该问题会使 `/scripts` 报价失败，必须在阶段 C 验收前解决，不能通过放宽全部字段校验掩盖。

## Pricing Registry 与模型可用性

唯一脚本积分事实源是 `config/script_pricing.json`；`ScriptModelRegistry.configs()` 读取模型与价格配置，`models()` 将目录能力和价格合并，`resolve()` 校验模型、思考强度、内容类型及非负整数价格。`ScriptService.config()` 将同一份结果提供给 `/api/scripts/config`，`quote()` 与 `submit()` 也通过 Registry 路由取价。

| 模型 | Low | Medium | High | Extra High | Max |
| --- | ---: | ---: | ---: | ---: | ---: |
| GPT-6 Astra | 30 | 35 | 40 | 45 | 50 |
| GPT-6.1 Sol | 10 | 15 | 20 | 25 | 30 |
| GPT-6 Luna | 免费（0） | 免费（0） | 免费（0） | 5 | 未定价 |

配置单位为 `one_finished_txt`；当前页面与请求合同每次生成一份 TXT，没有生成数量控件。最小范围继续明确显示“1 条 / 每次一份 TXT”，无需顺带新增批量生成。将来支持数量时，必须由后端计算数量乘单价，并覆盖每条任务和结算语义。

模型能力并非从 UI 名称推定：

- 本地 Registry 检查 Codex 可执行文件存在，以及当前 `models_cache.json` 的能力和新鲜度；目录最大有效期为 7 天。缺失、过期或没有支持强度时禁用。
- `resolve()` 拒绝目录没有的强度及未定价强度。手动选择不可用时不会替换模型；`run()` 在执行前再次验证所选模型。
- `RemoteScriptRegistry` 仅接受 120 秒内活跃 worker 的能力声明；worker 离线时不可用。队列按精确模型及强度派发。
- 本次在独立目录运行只读 `ScriptModelRegistry.models()`：三个模型的目录状态均为可用，全部 `inference_verified=false`。Astra/Sol 的 Ultra 未定价；Luna 的 Max 未定价。没有执行推理，因此不宣称模型实际生成已验证。
- 现有前端仅展示“暂不可用/暂未定价”，没有真正禁用相关 option；阶段 C 应阻止选择/提交不可用组合，并保留解释。

推荐路由先读取模板默认值，再执行本地 Python 规则；模糊需求在启用 `deepseek_flash` 后可能调用外部服务。当前配置该功能关闭。手动切换模型/强度的价格展示应直接读已加载的 Registry 数据，不能调用需求分析服务。若保留自动推荐预览，应明确保证该预览不触发外部模型；需调用模型的分析不应混入积分刷新路径。

## 两个页面的交互现状

`image_workbench/web_templates/scripts.html` / `web_static/scripts.js`：

- 独立页面已有 650ms 去抖自动 `/api/scripts/quote`，但保留“查看报价”按钮。
- `updateControls()` 要求 `state.quote.can_generate` 和请求完全匹配后才启用生成。
- 成功提交后调用 `invalidateQuote(false)`，文案要求再次查看报价；因此只删除按钮会让下一次生成卡住。
- 思考强度选项没有标注正常价格；价格卡片等到需求完整、报价成功才出现积分。
- 提交用持久化的请求指纹与 request_id 处理响应丢失/刷新，需保留。
- `generate()` 在等待补报价前尚未设置 `busy`；重构时应在第一个异步步骤之前锁定本次提交，避免新的一键流程双击竞态。

`image_workbench/web_templates/video.html` / `web_static/pipeline.js`：

- `pipelineScriptQuote` 是强制的手动“查看报价”步骤，完全没有自动报价。
- submit 明确要求已有 `p.quote` 且 body 一致，否则提示“请先查看当前需求的报价”。
- 思考强度选项已从 Registry 显示价格，但免费显示为“0 积分”，生成按钮文本为“生成 TXT”。
- 每次 invalidate 会替换 `p.scriptRequest`，目前没有和独立页面相同的跨刷新 pending 请求持久化。新增一键流程时要保留同一个内容请求的 request_id，尤其是提交响应丢失后。

两处需要同时修改；只改 `/scripts` 无法解决用户在视频工作台遇到的问题。统一按钮文案应为“开始生成”，模型/强度附近展示“每条积分、本次预计、当前余额”即可，保留现有布局风格。

## 复制基线的接口错配

`scripts.js::requestBody()` 当前会发送：

- `template_version`（模板带 version 时）
- `style_ids`
- `example_refs`
- `variables`
- `conflicts_confirmed`

但 `image_workbench/contracts/scripts.py::ScriptRequest` 没有以上字段，并通过 `extra="forbid"` 拒绝它们。`ScriptService._normalize()` 也没有纳入这些字段。离线使用 Pydantic 校验一个页面等价请求，得到 `style_ids`、`example_refs`、`variables`、`conflicts_confirmed` 四个 `extra_forbidden`，可确定 `/scripts/quote` 返回 422。该请求验证没有运行服务或写入数据。

同时，模板选择器已有“自由生成 · 不使用模板（none）”选项，但后端仍用现有模板 loader；整合时需检查该选项的合同与实际支持一致。

这是独立副本创建时已存在的跨层变更未完成状态，应先协调取齐现有创作资源改动，或做明确的当前版本兼容。不能只设置 `extra="ignore"` 或仅扩展 HTTP schema：风格/案例/变量必须进入规范化请求、指纹、报价签名、prompt、任务记录和版本信息，否则界面显示的选择不会真实生效。

## 积分与任务安全现状

已有保护应保持：

1. `ScriptService.submit()` 从后端 Registry 重新计算，前端不能提交 price；HTTP schema 拒绝未声明字段。余额不足在模型调用前拒绝。
2. task_id 按 account_id + request_id 确定性生成；请求指纹不同会冲突。同一个请求重试恢复同一个 reservation/outbox，不重复扣费。
3. `IdentityStore.reserve()` 在 SQLite 事务中核对余额、冻结金额和写入 outbox/ledger；同请求或活动任务复用已有记录。
4. 免费任务也执行 reservation 与 `freeze=0` / `consume=0` 账本，保留任务与使用日志，没有绕过任务体系。
5. `ScriptService.run()` 持有 task 锁，并核对 reservation 的 model_id、thinking_level、prompt、fingerprint 及金额；同一任务并发执行一次。
6. 完成后先校验/登记 TXT Artifact 与 Version，再正式结算。明确失败退款；不确定执行状态保留冻结，禁止自动重跑。归档失败可从持久候选正文恢复，不再调用模型。
7. `record_job()` 对已完成/退款终态幂等，迟到通知不能改写结算。远程执行也校验 claim、attempt 和结果 hash。
8. WorkspaceProjects / `_authorized()` 校验账号、Workspace、项目、子项目和版本来源。

需要补足的 C8 缺口：

- `CapacityPolicy` 的每账号待处理、全局待处理、每日积分预算默认均为 0，即关闭。
- 即便启用每日积分预算，也是对 `reservations.amount` 求和，免费任务永远不会消耗该预算。
- 当前没有按脚本提交次数及时间窗口限制的策略。账号登录限流只保护认证，不保护脚本生成。
- 本地仅对同一个 task 互斥，没有默认跨任务脚本并发上限。远程队列当前全局一次运行一个脚本，但仍不能替代提交频率限制。

建议将脚本次数窗口和脚本在途数量限制放入 `reserve()` 的同一个事务内，并提供明确、可配置的非零默认值。计数要包括 0 积分、已失败/退款提交；同一个幂等重试不占新次数。账号级策略应跨 Workspace 生效，避免切换 Workspace 绕过。保持图片/视频现有可选容量策略及正式价格不变。

实施时注意 `ScriptService.submit()` 当前先 `projects.create_task()`，再 `identity.reserve()`。容量拒绝可能留下没有 script job 的 task；不能新增限流后把这种残留视为成功任务。应使拒绝发生在持久化新任务之前，或使用可恢复的原子提交/回退设计，并补拒绝不留下可执行任务的测试。

## 脚本与分镜

本审计基线没有“同时生成分镜规划”选项，也没有自动分镜服务或分镜 Pricing Registry。现有的是：

- `ScriptService.storyboard_source()`：校验权限、固定 Version、读取已完成 TXT，提供下游输入合同，不调用模型、不生成分镜。
- 视频工作台：把当前脚本填入镜头描述、手动保存镜头、按视频服务规则提交 H3 视频。

因此本阶段不应新增一个假装免费的自动分镜调用。如果并行分镜任务后续整合了该选项，必须分别读取脚本与分镜的服务定价，并把分镜未配置状态显示为“分镜阶段价格尚未配置，可仅生成脚本”，不能从脚本 0 积分推断分镜免费。已明确的 Mock 模式应单独标记，不能当作真实服务可用。

## 建议最小修改

1. 保持 `config/script_pricing.json` 不变；两个页面用 `/api/scripts/config` 的本地数据即时显示每个强度的积分。将 0 显示为“免费”，不可用/未定价组合禁用。
2. 删除两个模板中的独立报价按钮和对应按钮事件依赖；生成可用性取决于有效输入、可用且已定价模型、余额和请求状态，而非是否点击过报价。
3. HTTP `ScriptSubmission.quote_signature` 改为可选；`submit()` 始终重新报价。若客户端仍提供签名，保留已有不匹配拒绝语义，避免静默接受过期价格。无签名请求也能安全提交，后端从不信任前端金额。
4. 保留 `/quote` 给推荐和质量提示使用；不要将其响应作为手动模型路径的必需操作。后端质量提示仍由现有确认语义处理，用户无需另点价格按钮。
5. 整合当前风格/案例合同错配，保持模板、风格、变量、版本和 pending 请求不被价格刷新清空。
6. 为免费脚本加入事务内次数限流和在途任务控制，复用 reservation/ledger，不增加新的积分系统。
7. 保留现有 TXT 格式、下载、Artifact、Version、失败恢复和分镜输入合同。

## 已有测试与建议补测

已有离线测试覆盖：14 项价格、目录过期/缺失、未定价强度、手动模型不替换、余额不足、固定模型、0 积分 reservation、重复提交、并发执行一次、冻结/退款、归档恢复、TXT hash、Version、跨账号/Workspace、远程 worker 与响应丢失。

主要文件：`test_script_models.py`、`test_script_service.py`、`test_script_web.py`、`test_script_remote.py`、`test_script_recovery.py`、`test_capacity.py`。本审计读取了测试，未运行完整测试套件，不能据此标记测试通过。

阶段 C 应补以下有行为价值的测试：

- 不带 quote_signature 直接提交，后端取 Registry 价格；伪造 price 字段被拒绝，提供过期签名仍拒绝。
- 两个实际浏览器入口没有报价按钮；模型/强度切换立即更新价格，免费/余额不足/未定价/不可用提示正确，切换不发模型请求。
- 两入口在不调用 `/quote` 的手动选择流程中完成生成；提交与刷新响应丢失后仍保持一次任务、一次推理和一次扣款。
- 一键生成从首个异步步骤即防重入；报价响应乱序不会改变最新选择，也不会使不可用模型按钮重新启用。
- 免费模型并发最后一个名额原子保护；完成/退款后仍受时间窗口次数限制；同 request_id 重试不重复占额；跨 Workspace 不能绕过账号限流。
- 容量/限流拒绝不调用 adapter、不冻结、不产生可执行残留任务；失败与成功日志完整。
- 页面等价的模板/风格/案例/变量请求经过 HTTP 合同、规范化、prompt 与版本记录，不会 422 或静默丢选择。
- 已生成历史 TXT 与手动编辑版本仍可查看、下载；输入、模板和风格不会因余额刷新被清空。
- 若后续有分镜选项：未配置分镜价格时显式阻止组合提交，同时允许仅脚本；Mock 与真实服务严格区分。

阶段 D 再运行相关完整回归与浏览器交互；所有 adapter 使用 Fake/Mock，禁止以真实收费生成验证 UI。
