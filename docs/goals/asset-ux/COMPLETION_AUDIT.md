# Asset UX 完成条件审计

结项更新：本文保留开发过程中的审计快照。所列五项补齐工作已完成；主目录完整检查共 639 项，635 项通过、4 项平台跳过，35 项浏览器通过。最终状态以 [FINAL_REPORT.md](FINAL_REPORT.md) 和 [TEST_REPORT.md](TEST_REPORT.md) 为准。

审计时间：2026-10-20 18:20（Asia/Shanghai）。

审计目录：`D:\ChatGPT\图片网站\work\asset-ux-20261020\source`。需求依据为用户附件《Image Workbench — Asset Interaction & Script Generation UX》原文，以及项目 `AGENTS.md` 的完整验证要求。本次仅阅读业务代码、测试代码、现有结果和 Git 差异；仅新增本审计文档，没有运行模型、访问生产、修改业务或测试。主 Agent 正在补齐分镜接线、脚本菜单及视频 Artifact，因此以下是明确时间点的验收快照，不是后续版本的失败判决。

## 结论

当前不能证明整个 Goal 完成。A/B 的基础输入、引用和隔离已有实现及阶段证据；C 的核心价格安全和直接生成已有实现与定向测试，但完整 C 报告在重跑期间被覆盖；D 的图片浏览器生成闭环、当前最终源代码完整回归及五份交付记录仍待完成。没有发现需要改变正式价目或接入真实付费模型才能补齐的事项。

最少补齐项是：

1. 完成并验证 A2 选中分镜粘贴，尤其首次默认选中镜头、其他编辑保留、保存及刷新、切换镜头期间上传完成的隔离。
2. 视频暂存区及绑定使用可由 `StorageManager.get_artifact()` 解析的稳定 Artifact；不得将 shot/job ID 别名测试当成 Artifact 登记证明。保留旧视频路径，验证无物理字节复制、重复登记幂等及权限。
3. 对刚加入的脚本“生成分镜 / 保存为模板”菜单执行浏览器流程，验证操作使用所点卡片的具体版本、保持项目/空间，模板确实保存，分镜确实进入明确 Mock 的任务流程。
4. 补一个浏览器图片 Mock 完整闭环：paste → 上传 Artifact → 任务引用同一 Artifact → 用户提交 → 本地 Mock Worker 完成 → 页面产物可见。现有图片浏览器套件只保存草稿。
5. 冻结代码后完成全部受影响浏览器套件与项目完整 `scripts/verify.py`；保存退出状态、数量、耗时、失败修复说明和源代码基线。随后更新五份必需记录和最终 15 项交付报告。

## 逐项依据

| 需求 | 当前实际证据 | 验收判断 |
| --- | --- | --- |
| 开发前审计与最小复用 | `PRICING_AUDIT.md`、`INTEGRATION_MAP.md`；输入复用 StorageManager、WorkspaceProjects 和原图片 Worker；脚本复用 Registry、Credit、Artifact/Version | 已有依据；旧审计里的“尚无分镜 / 创作资源接口错配”属于旧基线，不能作为当前状态 |
| A1 文件 / paste / 项目选图统一路径 | `asset-input.js::receive`、唯一 document paste 监听；`AssetLibrary.upload`；视频经 `PipelineService.upload_images` 同一路径 | 已实现；A 阶段 JSON 8 项通过，后端报告 13 项通过 |
| A2 图片生成 / 编辑 / 视频 | `pipeline.js::attachAssets`、图片页统一输入接线；编辑基底已有时选择用途而不覆盖；异步请求捕获空间、项目、draft revision | 已有代码及 A 浏览器测试 |
| A2 当前选中分镜 | `storyboard.js::assetTarget/attachAssets` 和 `pipeline.js::assetContext` 刚新增；支持引用、首尾帧及 dirty 内容保留 | 实现正在补齐，现有已保存 A/B 浏览器报告未覆盖。`loadRecord()` 首次设置首镜头为 selected，但 `assetTarget()` 还要求 boundShot 与 selected 相等；首次未点镜头按钮时需验证目标选择 |
| A3 文本 / 未知内容 / 重复 / Workspace / 失败 / 不覆盖 | paste 在文本框有 text/plain 时交给浏览器；没有图片不接管；上传协调器及元素 dataset 防重复监听；后端按子项目 SHA256 去重；权限由 scoped projects 校验 | A 输入和浏览器测试已有覆盖；保持新分镜分支的同等保护 |
| A4 后端格式、单文件、总量、张数 | `read_images` 限制 multipart；`AssetLibrary.upload` 限 1–9 张及 40 MiB、单文件限额，调用 `_inspect_image` 解码实际内容 | 已有代码与非法格式、损坏、超限、并发去重测试 |
| A5 阶段测试 | `TEST_REPORT.md` A 记录及 `browser-A.json`：8/8 passed、15.677 秒、page errors 0、模型调用 0 | 证明当时的 A 基线；尚不能证明刚新增分镜分支 |
| B1 项目素材暂存区 | `video.html` 折叠区；`reloadTray` 预览与菜单；`PipelineService.tray` 按子项目候选、hidden/recent 逻辑引用 | 图片及视频可见、选择、移出已有测试；移出仅改引用不删原件 |
| B2 图片不复制、Artifact 不变 | `AssetLibrary.require`、`VideoStore.link_artifacts`；`test_tray_image_keeps_artifact_id_when_used_by_image_and_video_tasks` 比较 ID 和磁盘素材快照 | 已有强证据 |
| B2 视频 Artifact 严格语义 | 审计读到 `_tray_candidates` 仍使用 `output.artifact_id or current_job.job_id or shot_id`；`bind_clip` 接受同一别名；旧测试明确断言 uploaded video ID = shot ID、generated ID ∈ job/shot ID | 尚未满足。主 Agent 正补登记；测试必须改为真实读取 MP4 Artifact，不能只比两个别名字段相等 |
| B3 图片 / 视频菜单 | `imageActions`、`videoActions` 提供实际支持的编辑、参考图、首尾帧、暂存区、下载，以及视频预览 / 浏览器首尾帧提取 / 绑定 | B 浏览器 7 项及后端 11 项报告已有基础证据；视频 Artifact 修复后需复核 |
| B3 脚本菜单 | 最新 `renderScripts` 已有继续修改、`planScript(task,'pipeline')`、`action=template`、下载；模板按钮按 `creative_resources` 能力显示 | 当前不是缺少代码；缺新入口到具体结果的浏览器证明。独立 `/scripts` 已有模板草稿、确认保存和版本能力可复用 |
| B4 图片到视频保持 Project/SubProject | `from_subproject` 复用映射，`import_image` 限当前子项目；菜单传 workspace/main/subproject/asset IDs | 后端并发映射与浏览器跨模块 handoff 有证据 |
| B5 暂存区拖拽 | 图片参考区、视频参考区已接自定义 Artifact MIME；分镜行刚接 dropzone | 前两项已有 B 测试；分镜新接线需补验证。用户允许复杂拖拽兼容限制留作优化，不要求引入新拖拽框架 |
| B6 刷新、历史、隔离、视频绑定 | 图片/视频移出后原文件保留；重开应用引用保持；主子项目 / Workspace / 账号拒绝测试；历史图片编辑和脚本历史 TXT 下载 | 基础覆盖已有；视频真实 Artifact 语义仍需补强后再宣告完整 B |
| C1–C4 自动积分及按钮 | `script-pricing.js` 只读 `/api/scripts/config` 数据；两个脚本入口没有独立 Quote 按钮；按模型/强度显示价格、免费、本次 1 条及余额；生成按钮“开始生成” | 代码已实现，并有“临时 Registry 改价后 UI 跟随”“quote 网络失败仍手动生成”浏览器用例 |
| C2 正式价目不修改 | 当前 `git diff -- config/script_pricing.json` 无差异；14 个已定价强度仍由 Registry 单点读取 | 不需要改正式定价。当前每次一份 TXT、无数量控件；不需为了“数量变化”举例新增批量生成 |
| C5–C7 服务端校验、余额、不可用模型 | `ScriptSubmission` extra=forbid，无前端 price；`submit` 重新 quote；可选旧签名若提供仍校验；`reserve` SQLite 事务；`run` 再次检查精确模型及冻结记录 | 已有实现；direct_submit 与浏览器用例覆盖伪造价格、余额不足、无签名提交、旧签名拒绝、不替换模型、失败退款 |
| C8 免费仍受保护 | `CapacityPolicy` 非零脚本默认 60 秒 / 6 次、每账号 3 个在途；`check_script` 在 reserve 同事务，含 0 积分、已完成及退款；跨 Workspace 共享账号额度；0 freeze/consume 账本 | `test_script_limits` 已包含窗口、重试、并发最后名额、重启及跨空间 13 项行为测试；需进入最终完整验证结果 |
| C9 一次提交、状态、选择、TXT、历史 | 两入口 busy 防重入、请求 fingerprint / request_id、持久 pending；TXT Artifact + Version；原版本下载不被编辑覆盖 | 存在真实浏览器 + Mock 正向闭环代码及测试；现有局部报告不能替代最终完整运行 |
| C10 分镜选项独立计价 | `ScriptService.quote` 单独取 storyboards quote，未定价不能组合执行；明确 Mock 选项；单独脚本仍可提交 | direct_submit 中含 unpriced rejection / mock independence / failure preserves script，浏览器含明确 Mock / real disabled |
| C11 阶段 C 全部通过 | `browser-C.json` 在审计期间由完整 13 项（6 failed）变为定向 5 项（全 passed），文件被当前修复重跑覆盖 | 不能据旧失败断言仍坏，也不能据最新 5 项宣称整套通过；需待当前代码稳定后完整 13 项报告和命令退出状态 |
| D1 前端交互 | A/B/C 已使用真实 Chromium + 临时 uvicorn；图片事件使用浏览器 File / ClipboardEvent / DataTransfer，文本另测真实剪贴板 Ctrl+V | 具备自动化条件；需补分镜、新菜单及图片完整生成。macOS 原生 Cmd+V 留作人工设备验收，不能写成已实测 |
| D2 后端全部回归 | 项目要求 `python -X utf8 scripts/verify.py`；该命令架构检查后 discover 所有 tests，并比对前后源代码 fingerprint | 当前五份记录尚无本轮最终完整退出 0 证据；需跑完整命令，不以脚本或图片局部套件推导全系统通过 |
| D3 实际耗时、不调用 AI | A/B JSON 有事件/流程耗时，C JSON 有本地价格切换耗时；价格由本地 Registry 计算、报价 route 设 `allow_external=False` | 机制正确；最终报告从最终完整 JSON 汇总实测值，不自行估算或复用已失效结果 |
| D4 异常反馈 | 上传非法/损坏、网络失败、项目切换、缺失/越权素材、余额、模型不可用、重复提交、刷新已有测试 | 新增分镜目标和视频登记失败分支需随各自测试覆盖；不需扩展为全站错误体系重写 |
| D5 保持风格 | 新组件复用原按钮、details、dialog、卡片样式；截图已存在 | 未发现必须全站重设计的缺口；实际截图需与最终测试一起核对 |
| D6 图片闭环 | `test_asset_browser` tearDown 要求 `self.model.generate.assert_not_called()`；`test_root_upload_paste_drop_are_direct_references_and_save` 止于保存任务。后端 `test_local_worker_receives_referenced_input_and_uploaded_edit_base` 已证明独立的 Mock Worker 能读引用并产出 | 不能把两段相互独立的测试写成完整浏览器闭环。应加一条从 paste 连续到 Mock 输出可见的测试 |
| D6 脚本闭环 | `test_scripts_template_style_txt_artifact_version_and_refresh` 选择模板/风格/模型/强度、提交、校验 TXT Artifact/Version/下载、编辑 v2、刷新并读取 v1；pipeline 入口也有生成和用于项目 | 验收范围覆盖正确；最终全套通过证据尚需保存 |
| 五份记录及最终交付 | `PROGRESS.md` 仍 C 进行中 / D 待开始；`TEST_REPORT.md` 有重复 B、C/D 待执行；`FINAL_REPORT.md` 只有开发中占位；`DECISIONS/BLOCKERS` 未包含当前集成后的实际状态 | 必需更新；审计文档不替代原要求的五份记录 |

## 最终验证应留下的具体证据

- 选中分镜粘贴：只新增一次 Artifact；引用落在目标镜头；原字段/已有素材不被覆盖；保存、重载后 ID 一致；旧版本只读、切换项目/镜头不串写。
- 视频 Artifact：上传及生成两类 MP4 均能 `storage.get_artifact(id)`；格式/大小/hash 正确；其 task placement 归当前子项目；重复查询/登记 ID 稳定；绑定与预览不另存一份物理内容；旧 URL 及原视频仍可访问。
- 脚本菜单：保存模板深链确实加载指定 task/version 后打开草稿，确认后可重新读取；生成分镜沿指定 source version 单独 quote，在明确 Mock 模式完成且不调用真实 planner。
- 图片闭环：记录上传 Artifact ID、任务 reference ID、Mock 收到的文件内容、输出 Artifact 和页面状态；Mock 调用计数恰为一次，真实模型计数为零。
- 完整 `scripts/verify.py` 退出 0，期间 source fingerprint 未变；本次新增测试包括在 discover 中；将失败与修复后的重新运行区分记录。
- 更新 `PROGRESS.md`、`DECISIONS.md`、`TEST_REPORT.md`、`BLOCKERS.md`、`FINAL_REPORT.md`。最终报告覆盖用户指定的 15 项，明确本地测试目录、文件清单、人工 macOS/系统剪贴板验收项、UI 待审批点以及“未 push、未部署，等待用户审批上线”。

## 不应增加的范围

无需新增批量脚本生成、重做整个工作台、另建存储/积分体系、扩大跨项目分享权限或调用真实收费模型来证明本次 UX。本地验收完成与上线审批是不同结果：本 Goal 应交付已验证的本地成果，保留上线待用户批准；不能把“等待上线”误写为本地开发阻塞。
