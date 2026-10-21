# 并行改动集成映射

## 16:58 最新复查（覆盖下文 16:24 的未装配结论）

2026-10-20 16:58 再次只读比较：**原目录的创作资源和分镜现已完成主要代码接线**。此前“创作资源字段仍被拒绝”的结论已过时；下文保留为早期集成记录。本次未运行测试，不将“已接线”表述为验收通过。

创作资源已贯通：`contracts/scripts.py` 声明 template_version、style_ids、example_refs、variables、conflicts_confirmed；`ScriptService` 创建 `ScriptResourceStore`，严格校验这些字段，统一规范化空默认值以兼容旧请求指纹，通过 `resolve_resources()` 准备模板/风格/案例，使用 `resource_summary()` 生成报价签名，冻结 `creative_snapshot` 与 prompt，并在 Version 保存资源来源、价格和交易标识。归档时执行不重复调用模型的结构提示检查。

资源 HTTP 的装配方式发生变化：现在是 `script_web.install_scripts()` 调用 `install_script_resources(app, get_service)`，资源路由按操作读取 `get_service().resources`。因此 composition 没有直接调用该 installer **并不是缺少装配**。前端也增加能力检测，旧服务不支持创作字段时不会盲目发送。

分镜通过 composition 构造并赋给 `script_service.storyboards`；有严格合同、独立定价、脚本完成后联动、视频绑定与 JSON/MP4 Artifact。`storyboard_web` 新增 lifespan 恢复，读取 `completed_storyboard_dependencies()` 补齐已完成脚本与尚未提交分镜的间隙。真实分镜仍为禁用/未定价，Mock 明确启用。

当前 source 为阶段 A 提交 `60ee15c` 加阶段 B 工作改动，后端脚本合同与服务仍基于 `310f7d8`；不能用它们覆盖原目录已完成的资源/分镜接线。阶段 C 应在取齐下面依赖后，继续去掉两处报价按钮与 mandatory quote_signature，保留资源冲突、两阶段金额、版本变更和幂等检查。

### 必须取齐的最小文件组

以下路径相对项目根。标为“合并”的文件已有双方修改，不能直接覆盖；其余也应依据 hash/三方差异确认后取入。

| 目的 | 必需文件 |
| --- | --- |
| 脚本创作资源完整链路 | `image_workbench/contracts/scripts.py`、`script_service.py`、`script_engine.py`、`script_resources.py`、`script_resource_web.py`、`script_web.py`、`script_templates.py` |
| 模板和风格数据 | `skills/video-script-generation/registry/templates.json`、`registry/styles.json`；新增 `templates/scripts/{brand_promo,character_story,commercial_ad,documentary,product_intro,social_short_video,storyboard_friendly}.txt` 与 `templates/screenplays/{ai_short_drama,ai_story_short,microfilm}.txt` |
| 文档解析依赖 | `requirements-web.txt` 新增 python-docx、pypdf；不需要改正式积分配置 |
| 分镜服务与合同 | `image_workbench/contracts/storyboards.py`、`storyboard_models.py`、`storyboard_planner.py`、`storyboard_service.py`、`storyboard_web.py`、`config/storyboard_pricing.json`、`skills/storyboard-planning/SKILL.md` |
| 装配与跨模块存储 | 合并 `image_workbench/composition.py`、`video_store.py`；取齐 `storage.py`、`project_manager.py`、`workspace_scope.py`、`accounts_web.py`、`http_api/artifacts.py` |
| 原目录上述文件的新增导入依赖 | `image_workbench/media_validation.py`、`contracts/images.py`。若同时取原目录的 `script_models.py/script_adapter.py`，必须一起取 `codex_runtime.py`；后两者主要是公共运行时抽取，不是创作字段本身的必要改造 |
| 前端完整入口 | 合并 `web_static/scripts.js`、`pipeline.js`、`web_templates/video.html`；取齐 `web_templates/scripts.html`、`web_static/scripts.css`、`storyboard.js`、`storyboard.css`；保留 source 的 asset-actions 与 asset-input 钩子和加载 |
| 必需回归 | `tests/test_script_creative_http.py`、`test_script_creative_core.py`、`test_storyboard_http.py`、`test_storyboard_recovery.py`、`test_storyboard_planner.py`；更新当前合并结果的 HTTP 合同快照，不能直接用根快照覆盖本任务新增路由 |

不能仅复制 `scripts.js` 和 schema：资源准备、指纹、prompt、冻结快照、Version 必须是同一版。也不能仅复制 storyboard_service：document Artifact、账号路由、项目类型和视频能力是它的运行依赖。

### 相比 16:24 新出现的合并点

- 根 `PipelineVideoPort` 把 `storage` 访问改为 `max_image_bytes` property，并新增 `associate_artifact()`。`import_image()` 现在复制视频素材后关联原 Artifact。source 已有 `link_artifacts()` 无复制方案，应保留此方案并采用新的容量 property；不要恢复复制路径，仅在旧素材兼容需要时保留 associate_artifact。
- 根 `VideoStore` 增加上面的 property/关联端口、分镜输出恢复等。source 的 `_asset_path/link_artifacts` 仍需保留；根 `storyboard_service.video_draft()` 的读取文件再 add_assets 路径仍需结合本任务改为逻辑引用。
- 根新增 ProjectManager 的 asset/storyboard/video 操作类型及图片格式统计白名单，WorkspaceStorage 放行 `register_output_document()`；遗漏会导致分镜服务创建/归档失败。
- 根 `scripts.js` 已有重新生成时恢复风格/变量、模板/案例管理、结果 validation_report、分镜阶段状态轮询及来源 Version 入口；source 阶段 B 新增的素材操作菜单应逐段合并。
- `video.html` 需先加载 storyboard.js 再加载 video.js/pipeline.js，同时保留 source 的统一素材输入与操作模块。pipeline.js 的请求指纹必须包含 storyboard_options，按钮简化后仍要展示两阶段价格。
- 根 architecture.json 现在已登记 script_resource_web 与 storyboard 模块；早期报告关于这两类未登记的观察已过时。本任务 asset_library/image_references 与新增依赖边仍需单独核对。

最新测试定义数量为创作资源 HTTP 23 项、core 7 项；分镜 HTTP 28 项、recovery 20 项、planner 6 项。新增恢复测试包含已付费规划结算、幂等并发、启动补偿、取消退款、输入身份及媒体缺失。这里仅核实测试定义和 Mock 使用，未执行或引用其他聊天的通过声明。

16:58 原始文件 SHA-256 前缀：contracts/scripts `157e6fee081ef883`；script_service `24127e21a3558db4`；script_engine `3fb689bbe88ca81f`；script_web `2f78aecd7388c6bb`；storyboard_service `a1fa9d730eb0f668`；composition `6cf732e72352d002`；video_store `2fe3310529f22d18`；scripts.js `d01d01c01ea01a52`；pipeline.js `3bc35dda9fefb307`。原目录仍可继续变化，实际取入前应重新核对。

审查日期：2026-10-20，主要快照时间 16:24（Asia/Shanghai）。

比较对象：

- 基线 B：独立目录的本地 Git 提交 `310f7d8`。
- 本任务 O：`D:\ChatGPT\图片网站\work\asset-ux-20261020\source` 的当前素材交互改动。
- 并行改动 T：`D:\ChatGPT\图片网站` 的当前源文件。

本报告只读检查源代码、函数结构、合同及测试定义，仅写此文档。未修改业务代码，未启动服务、运行模型或执行测试套件，未联系其他聊天。原目录在审查期间持续发生变化，因此这是逐文件采样记录，不是对原目录完成状态的承诺。

## 当前完成程度

截至最后一轮主要快照，分镜已从“只有 UI 和测试”推进到服务装配；脚本创作资源仍未完成贯通。两者不能合并为一句“脚本系统已完成”。

| 层级 | 原目录最新观察 | 当前判断 |
| --- | --- | --- |
| 脚本请求合同 | `contracts/scripts.py` 已增加 `storyboard_options: PlanningOptions` 和 `storyboard_confirmed`；仍没有 `template_version/style_ids/example_refs/variables/conflicts_confirmed` | 分镜字段已接入，创作资源字段仍被 `extra="forbid"` 拒绝 |
| 创作资源 engine | `script_engine.py` 有 `resolve_resources/resource_summary/creative_snapshot/recommend/check_structure`；`script_templates.py` 已加 `list_styles`、变量、风格、受限案例片段的 prompt 支持 | 功能函数存在，但 ScriptService 尚未调用这些 engine 函数 |
| 创作资源 HTTP | `script_resource_web.py::install_script_resources` 存在；其 context 依赖 `application.script_service.resources` | composition 未调用 installer，ScriptService 未装配 resources；不能认为端点已可用 |
| 脚本 Service | 已支持分镜参数规范化、组合报价、提交确认、脚本归档后 `dependency_completed`、公开下游状态 | 分镜链路已接线；仍使用旧模板 loader 和旧脚本参数名单，创作资源缺口仍在 |
| 分镜 Service / HTTP | 新增 `storyboard_models.py/planner.py/service.py/web.py` 和 `contracts/storyboards.py`；composition 创建 StoryboardService 并安装路由 | 主要入口已连接，未执行测试，不能认定闭环通过 |
| 分镜定价 | 新增 `config/storyboard_pricing.json`；`mock_enabled=true`、`real_enabled=false`、`free_test_enabled=false`、`models={}` | 只明确启用 Mock；真实分镜未定价/未启用，不能沿用脚本积分 |
| 视频衔接 | VideoStore 新增 `video_models` 能力、`storyboard_binding`、下游任务绑定、MP4 Artifact 登记 | 已有接线；与本任务“不复制素材”的路径存在语义冲突，详见下文 |
| 存储与鉴权 | 原目录 StorageManager 新增 JSON/MP4、storyboard/video/asset operation；Artifact HTTP 增加媒体类型；账号路由已接纳 `/api/storyboards` | 必须作为分镜依赖包整体考虑，不能仅复制 storyboard_service |
| 前端 | scripts.js 已扩展创作资源管理，两个脚本入口已增加 `IWStoryboard` 挂载、参数与报价展示；仍保留报价按钮 | 前端功能量超出当前后端创作资源合同，仍需阶段 C 修复与整合 |

本报告前期曾观察到分镜尚未装配，后续 16:24 快照已变更；以上表格取后一次证据，避免用早期状态覆盖新改动。

### 脚本与分镜当前实际接线

`composition.create_app()` 先构造 ScriptService、VideoStore、PipelineService，再构造 StoryboardService、赋值 `script_service.storyboards` 并安装 `install_storyboards`。远程模式的 Registry 替换发生在 StoryboardService 创建之前。

`ScriptService.quote()` 在脚本报价之外读取 `storyboards.quote_options()`，返回 `storyboard_quote`、`total_estimated_credit` 并组合 quote_signature。`submit()` 验证两阶段报价及 `storyboard_confirmed`，脚本仍只冻结自身金额；分镜在脚本 TXT/Version 提交成功后另行提交。`_archive()` 的下游调用发生在脚本完成与结算之后，分镜失败不能把已完成脚本改为失败。

这与阶段 C 的“后端独立取两项价格”方向兼容。移除前端报价按钮时必须保留：

- 脚本单价、分镜单价及合计的明确展示。
- 分镜真实价格缺失时的阻止逻辑，以及取消分镜后仅生成脚本的路径。
- 两阶段分别幂等、分别冻结/结算、固定脚本 Version 的语义。
- 可选签名或自动校验的改造不能悄悄绕过分镜未定价、余额不足或资源版本变化检查。

当前创作资源仍缺 `ScriptRequest → _normalize → resolve_resources → resource_summary/quote → creative_snapshot/job/version → build_prompt` 的完整接线。不能只给 Pydantic 增加字段后就宣称风格与案例已生效。

## 与本任务修改的文件映射

表中“根新增”均指相对 B，而非相对本任务当前文件。ImageQueries、PipelineService、composition、contracts 等主要解耦在 B 中已经存在，不应再次手工拆分。

| 文件 | 原目录 T 相对 B | 本任务 O 相对 B | 合并要求 |
| --- | --- | --- | --- |
| `workbench_state.py` | `approve()` 从拒绝 TXT 改为仅批准 PNG/JPEG/WEBP output | `create_task()` 接收 `reference_artifact_ids`；验证子项目、数量、大小；提交禁止素材登记任务；积分计算计入逻辑引用 | 函数基本不重叠。保留 T 的图片格式白名单，叠加 O 的引用逻辑；不要恢复整份旧 WorkbenchState |
| `image_queries.py` | 快照与 B 一致 | `task_view()` 合并逻辑引用；`project_views()` 隐藏素材登记用任务 | 保留 O；该查询层已由 WorkbenchState 委派调用，不要把查询逻辑重新移回主类 |
| `task_runner.py` | 快照与 B 一致 | `_execute()` 从统一 `task_input_artifacts()` 获取物理输入与 Artifact 引用 | 保留 O；检查完整输入列表、hash 和本地执行仍使用原任务/产物体系 |
| `remote_queue.py` | 快照与 B 一致；基础 contracts/atomic helper 抽取已在 B | `_manifest()` 纳入 `task_input_artifacts()` | 保留 O；远程资产下载必须允许 manifest 中的逻辑引用，并继续验证 claim 和项目归属 |
| `pipeline_service.py` | 快照与 B 一致；根新增独立单测 | 新增 `upload_images()`；图片输入与送入视频改用 AssetLibrary；`PipelineVideoPort` 增加 `link_artifacts()` | 保留 O 的端口能力与无复制语义，补齐根独立 Mock(spec=PipelineVideoPort) 测试；不要恢复旧 add_assets 复制导入 |
| `video_store.py` | `inspect_mp4/MAX_OUTPUT_BYTES` 抽出到 media_validation；新增视频能力、storyboard_binding、下游元数据、MP4 Artifact 登记 | `_asset_path()` 支持 Artifact 别名与归属检查；新增 `link_artifacts()`；上传总大小限制 | 同时保留。抽取与 O 方法通常不重叠；分镜素材映射必须统一走 link_artifacts，见下一节 |
| `composition.py` | 装配 StoryboardService 及其 HTTP；尚未安装创作资源 HTTP | `install_video()` 增加 PipelineService 参数，使视频上传复用资产登记 | 保留 O 的参数注入，再添加 T 的分镜装配。不能整文件择一 |
| `workbench.js` | 抽出格式、API、预览、账号四个组件，主文件从约 1113 行降到 975 行 | 新增 referenceArtifacts、粘贴/拖拽接收、项目图片选择、发送 Artifact ID | 以 T 的组件化入口为骨架，重放 O 的素材状态和事件；移除旧上传/拖拽监听只保留统一接收器，不能同时保留两套监听 |
| `web_templates/index.html` | 加载四个 workbench-* 组件，顺序必须在 workbench.js 前 | 加 asset-input.css/js、粘贴说明与项目素材选择按钮 | 合并 script/link 标签与 UI，两类组件均只加载一次 |
| `http_api/workspace.py` | 缓存版本计算纳入四个组件文件 | 安装素材接口/资源引用相关改动 | 缓存版本应纳入最终页面真正依赖的新增文件，保留路由安装 |
| `web_templates/video.html` | 加 storyboard.css/js | 加素材输入 CSS/JS、粘贴目标、已有素材选择等 | 当前模板多为长单行，Git 文本冲突不能通过整行择一解决；逐标签保留双方入口和加载顺序 |
| `web_static/pipeline.js` | `body()` 加 storyboard_options；挂载分镜模块；结果卡加规划按钮；报价/提交传递分镜字段 | 素材输入统一登记、Artifact 引用及跨模块图片流程 | 保留 O 的素材流程和 T 的分镜钩子，再共同改造一键生成。请求指纹必须包含分镜选项 |
| `web_static/scripts.js` | 创作资源 UI、分镜钩子大量新增，尚未与后端创作资源合同贯通 | 阶段 C 尚待实施 | 不以独立副本旧 JS 覆盖根新增 UI；价格重构应对完整资源请求和分镜请求同时工作 |

### 新抽取模块必须成套保留

- `workbench-format.js` 暴露 `WorkbenchFormat`；主文件通过解构读取日期、体积、状态等格式化函数。
- `workbench-api.js` 暴露 `WorkbenchApi.create()`，通过回调读取当前 Workspace/CSRF，负责请求和登录失效处理。
- `workbench-preview.js` 暴露 `WorkbenchPreview.create()`，拥有预览版本、关闭状态和事件；主文件只保留返回的 open/close 函数，避免重复预览监听。
- `workbench-accounts.js` 暴露 `WorkbenchAccounts.create()`，拥有账号弹窗、Workspace 切换、积分记录及定时刷新事件。
- `media_validation.py` 拥有 MP4 检查和大小常量；`video_store.py` 当前仍导入并暴露旧名称，兼容现有 `from video_store import inspect_mp4` 测试。
- `codex_runtime.py` 拥有 `_codex_executable/_codex_home/_child_environment/_stop_process/_DISABLED_FEATURES`。ScriptAdapter/ModelRegistry 改为从此读取，CodexImageBackend 保留导入名称。素材改动不应把这些函数复制回旧文件。
- `contracts/images.py` 从 HTTP schema 位置提取通用图片合同；HTTP handlers 与兼容入口一起更新。本任务新增字段若曾加在旧 schema 路径，需要迁移到实际生效合同，不能留下两个不一致定义。

## 分镜素材路径的直接冲突

根 `storyboard_service.py::_assets()` 为没有 artifact_id 的旧视频 asset 创建 StorageManager input Artifact，随后把 artifact_id 回填到视频 asset。这可以作为历史数据首次登记的兼容分支；对本任务已通过 AssetLibrary 登记的素材，必须直接使用已有 artifact_id。

根 `video_draft()` 当前遇到没有匹配视频 asset 的 Artifact 时，会读取物理文件并调用 `video.add_assets(...)`，然后回填 artifact_id。该路径会复制已经登记的图片，不符合本任务的 B2。

安全整合方式是优先调用 `VideoStore.link_artifacts()` 建立逻辑别名，保持 artifact_id 不变；仅旧媒体没有 Artifact 身份时允许一次明确的兼容登记。首帧/尾帧/参考图仍由镜头中原始 Artifact ID 标识，视频 asset_id 只是服务内引用。不得因列表刷新或创建视频草稿重复写图片。

同样需要保留根新增的 storyboard_id、storyboard_version、storyboard_shot_id、shot_fingerprint、source_script_version 关联。这些关联解决版本变更后的历史视频识别问题，与素材暂存区是两种不同状态，不能互相替代。

## 根目录新增测试是否覆盖脚本字段

有针对性的测试定义，尚无本次审查运行结果。

`tests/test_script_creative_http.py` 共有 23 个测试，复用现有 ScriptWebTests 的本地临时 Storage 与 Mock adapter。覆盖：

- `test_free_generation_combines_styles_example_and_user_constraints_at_old_price`：请求含 style_ids、example_refs（含 version/reference_roles）、variables、conflicts_confirmed，断言 prompt 包含风格/案例/变量、creative_snapshot 固定来源、仍按原价格扣款且只调用一次 Mock。
- `test_conflicting_styles_require_explicit_confirmation_and_preserve_user_constraint`：确认前 409、不冻结、不调用模型；确认后保留用户明确约束。
- `test_quote_changes_after_personal_or_example_version_change`：模板或案例更新令旧 quote_signature 无效。
- `test_queued_task_freezes_old_template_example_and_prompt_after_edit_disable`：显式 template_version 与 example version 固定；排队后编辑/停用资源不会改变 prompt，Version 保存资源来源。
- 其余测试覆盖跨账号/Workspace、文档上传解析与原件保留、资源版本、草稿确认、未知引用角色、CSRF 与免费规则推荐。

这些用例明确覆盖新字段的预期行为，但当前 ScriptRequest 与 ScriptService 未接线，测试存在不能证明接口已经通过。尤其不能只跑旧 `test_script_web.py` 就漏掉新字段拒绝问题。

`tests/test_storyboard_http.py` 共有 27 个测试，使用本地存储、Mock 脚本 adapter、默认 Mock 分镜；真实 planner adapter 设置为一旦误调用就失败。覆盖脚本联动、脚本/规划失败、固定历史 Version、编辑/拆分/合并/排序、草稿视频、历史视频过期标记、未定价、积分不足、幂等及 Workspace/CSRF。

分镜新增测试应与本任务新增素材测试一起运行，尤其增加“已有 Artifact → 分镜参考图/首尾帧 → 视频草稿”不复制物理文件和 ID 不变的联合用例。

根还新增：

- `test_pipeline_service.py`：5 个端口级用例；拒绝未授权跨域读取、固定脚本版本、拒绝损坏图片、重复引用不建任务。
- `test_frontend_modules.py` 和 `tests/frontend/workbench.test.js`：前端拆分组件行为与全部 JS 语法。
- `test_architecture.py`、`architecture.json`、`scripts/check_architecture.py`：模块分组、跨层导入、循环、前端组件加载约束。

架构配置当前没有完整登记 storyboard 与 script_resource_web，也没有本任务新增 asset_library/image_references。既有规则还限制 pipeline 直接依赖图片层、video 直接依赖 projects。合并后应按模块实际职责新增精确分组/允许边或调整注入接口，并执行架构检查；不能关闭检查或放宽成全模块互相导入。

## 推荐三方合并方式

1. 保留当前独立 source 开发与阶段提交，以 B 为共同祖先。继续阶段 A/B/C 工作，不因原目录尚在写入停止独立工作。
2. 需要集成时，在独立本地集成目录取得原目录的一份完整代码快照 T，记录文件 hash；若复制前后 hash 改变，重新读取该文件及其关联合同。只纳入代码、配置、技能资源与测试；排除 data、生产数据库、工作缓存、日志、虚拟环境和测试临时目录。
3. 将 T 作为从 B 出发的本地快照提交，O 作为素材任务提交，在独立的 `codex/` 集成分支执行三方合并。不要在正在写入的原目录直接覆盖文件，也不要把当前 source 的未提交工作切换到另一分支。
4. 对单方修改文件保留该方变化；对表格中的公共文件按函数/调用边界合并。长单行 HTML、scripts.js、pipeline.js、composition、ScriptService 是人工检查重点；不使用整文件“ours/theirs”解决。
5. 脚本资源以一组完整依赖合入：contracts、resources、engine、templates、resource HTTP、service、composition、registries、前端和 23 项测试。分镜以另一组完整依赖合入：contracts、models/planner/service/web、脚本挂钩、storage/document Artifact、video binding、鉴权、配置、前端和 27 项测试。
6. 合并后先验证导入、HTTP/OpenAPI 合同、JS 语法与架构，再执行脚本资源/分镜/素材行为测试；最后跑账户、Workspace、Storage、TaskRunner、Credit、Pipeline/Video 回归和两个浏览器闭环。所有 adapter 保持 Fake/Mock。
7. 仅在本地验证通过后生成待审批整合结果；本报告不执行推送、部署、生产数据迁移或修改正式价格。

## 关键采样指纹

以下为 16:24 一轮读取的规范化 UTF-8 文本 SHA-256 前 16 位，用于识别后续是否又有并行修改；它们不代替最终完整快照。

| 文件 | 指纹 |
| --- | --- |
| `contracts/scripts.py` | `75ebcca323d9654c` |
| `script_service.py` | `22bc17be3248b32f` |
| `composition.py` | `2949d7ac0f3cce4a` |
| `script_engine.py` | `a4bcf487b2173f84` |
| `script_resource_web.py` | `58513abf0410b9d1` |
| `storyboard_service.py` | `326316e01a6756ba` |
| `storyboard_web.py` | `93ff661d5d38fcf6` |
| `video_store.py` | `c206a8f69e3c159b` |
| `workbench.js` | `d196b4197b45cfe5` |
| `pipeline.js` | `8f19f01db6da98d0` |
| `scripts.js` | `532fd36023c4aa4a` |
| `tests/test_script_creative_http.py` | `85450cdb4ee8231b` |
| `tests/test_storyboard_http.py` | `83d650260d353c8c` |
