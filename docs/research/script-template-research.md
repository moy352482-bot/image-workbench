# 脚本模板与风格引擎：开源研究及本地预设

核实日期：2026-10-20（Asia/Shanghai）。本报告基于公开 GitHub 仓库 API、官方 README、许可证、实现与测试文件；未按 Star 数筛选。研究只借鉴设计，不复制外部项目的大段代码、提示词或作品，不接入其云服务，也不运行付费模型。

## 结论与采用边界

沿用现有 ScriptService、模型注册表、Codex Adapter、存储、版本与积分流程。模板结构与表达风格分开登记，引用时记录整数版本和内容摘要；推荐只给建议，分析先形成草稿并经用户确认。上传作品仅提炼结构、风格、对白或格式，不将全文转换成模板，不把引用内容当成任务指令。

本轮采用 12 个系统模板（原有 2 个兼容保留、新增 10 个）和 15 个风格。预设均为本项目编写的结构规则；`examples` 是原创微型示例，不冒充用户优秀案例。不存在必须安装的新增框架。`config/script_routing.json` 的 `deepseek_flash.enabled=false` 已由本地文件核实；规则分析和可注入 Mock 适合当前免费预览，界面应如实呈现分析方式。

## 项目比较

下表的维护证据是核实时点的默认分支提交与仓库状态，不能保证未来维护或项目质量。所有仓库核实时均 `archived=false`。未执行这些外部仓库的测试；“测试证据”指阅读了官方测试，Dramatron 的缺项单独注明。

| 官方仓库 | 许可证与维护证据 | 优点、限制 | 本项目采用决策 |
| --- | --- | --- | --- |
| [LangChain](https://github.com/langchain-ai/langchain) | [MIT](https://github.com/langchain-ai/langchain/blob/master/LICENSE)；默认分支提交 `afafd55d34cb`，2026-10-20 UTC | 提示模板将内容、变量与校验分离，测试覆盖缺参、格式与文件读取；完整框架远超本轮需要，接入模型仍受提供商条件影响 | 借鉴显式必填字段、部分变量与加载验证；继续使用现有加载器，不引入 LangChain 依赖 |
| [Langfuse](https://github.com/langfuse/langfuse) | [根许可证](https://github.com/langfuse/langfuse/blob/main/LICENSE) 的普通区域为 MIT Expat；`ee/`、`web/src/ee/`、`worker/src/ee/` 等适用 [Enterprise License](https://github.com/langfuse/langfuse/blob/main/ee/LICENSE)；默认分支提交 `afbc88d3ff17`，2026-10-20 UTC | 提示词版本、标签、变量与可观察性较完整；自托管基础设施和项目权限体系比本项目需求重，不能笼统视整库为 MIT | 借鉴版本与可移动标签的区别；生成任务固定具体版本及内容快照，不依赖会变化的“最新”标签；不复制企业区域代码或部署 Langfuse |
| [Promptfoo](https://github.com/promptfoo/promptfoo) | [MIT](https://github.com/promptfoo/promptfoo/blob/main/LICENSE)；默认分支提交 `b49d55dfdec2`，2026-10-19 UTC | 声明式用例、断言与多提供商评估适合回归；模型评分不是免费或必然正确，远程提供商会收到输入 | 借鉴输入样例加确定性断言；采用本地结构检查和 Mock 验证，避免为缺项自动重复收费调用；暂不安装评估 CLI |
| [Dramatron](https://github.com/google-deepmind/dramatron) | [Apache-2.0](https://github.com/google-deepmind/dramatron/blob/main/LICENSE)；默认分支提交 `2e7c36afadac`，2024-07-16 UTC | 从一句梗概分层形成角色、场景与对白，适合作为创作结构参考；代码集中在 Colab，接口和研究假设较旧，未找到独立自动化测试目录 | 只借鉴人物目标→事件因果→场景动作的层级，不导入 Notebook、外部模型调用或完整提示词；不强迫每位作者按固定创作顺序操作 |
| [python-docx](https://github.com/python-openxml/python-docx) | [MIT](https://github.com/python-openxml/python-docx/blob/master/LICENSE)；默认分支提交 `e45454602b53`，2025-06-16 UTC；仓库 `pushed_at` 为 2026-08-01，两者分别记录，不能据此宣称默认分支持续更新 | 可读取、创建和更新 Word 2007+ `.docx`；有段落与表格内容遍历及测试；不是 Word 渲染器，不能直接解析旧二进制 `.doc`，修订标记及复杂对象需单独处理 | 使用已安装依赖解析允许的 `.docx`；尽可能按正文顺序遍历段落和表格，保留原件；不承诺还原分页、浮动文本框或全部修订内容 |
| [pypdf](https://github.com/py-pdf/pypdf) | [BSD 三条款正文](https://github.com/py-pdf/pypdf/blob/main/LICENSE)；GitHub API 许可证检测为 Other，实际按 LICENSE 核实；默认分支提交 `09dd269d7be1`，2026-10-19 UTC | 纯 Python，可读 PDF 并提取文本，有多语言和布局相关测试；阅读顺序和版面语义不保证准确，扫描图像没有文字层时不会自动 OCR | 使用已安装依赖读取带文本层 PDF；空文本给出明确失败或提示，不补造内容，不引入 OCR 或上传到外部服务 |

若今后分发上述依赖或引入代码，应保留对应版权、许可和免责声明；Langfuse 必须按实际目录识别授权范围。此次预设不包含外部代码和提示词，因此不将任何外部仓库作为模板来源。

## 官方实现和测试依据

### LangChain：模板数据与变量校验

已阅读 [README](https://github.com/langchain-ai/langchain/blob/master/README.md)、[PromptTemplate 实现](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/prompts/prompt.py) 与 [模板测试](https://github.com/langchain-ai/langchain/blob/master/libs/core/tests/unit_tests/prompts/test_prompt.py)。实现显式声明 `input_variables`、`partial_variables`、`template_format` 和 `validate_template`；测试覆盖合法模板、缺少输入、错误变量及文件编码。代码同时提示不可信 Jinja2 模板的风险。

适配：模板字段只作为数据，不开放任意模板代码执行。`required_fields` 初始仅 `user_request`，时长、受众等放入可选字段，避免空值迫使用户填写无关信息。编译时只合并实际存在的数据，不能把示例中的事实带入本次作品。

### Langfuse：整数版本与可移动标签

已阅读 [README](https://github.com/langfuse/langfuse/blob/main/README.md)、[Prisma 数据定义](https://github.com/langfuse/langfuse/blob/main/packages/shared/prisma/schema.prisma)、[模板字符串工具](https://github.com/langfuse/langfuse/blob/main/packages/shared/src/utils/prompts.ts)、[名称校验测试](https://github.com/langfuse/langfuse/blob/main/packages/shared/src/features/prompts/validation.test.ts) 和 [v2 提示 API 测试](https://github.com/langfuse/langfuse/blob/main/web/src/__tests__/server/prompts.v2.servertest.ts)。`Prompt` 以项目、名称、整数版本形成唯一约束；API 测试覆盖按版本取用、标签查询和标签迁移，说明标签与历史版本并非同一概念。

适配：系统模板、个人模板、案例与风格引用都带 `version`；用户编辑生成新版，停用保留历史，报价与排队任务固定实际引用。个人资源用 account + workspace 隔离，不能借用外部产品的权限假设。暂不需要标签系统或独立观测平台。

### Promptfoo：可复现断言与模型评分分离

已阅读 [README](https://github.com/promptfoo/promptfoo/blob/main/README.md)、[断言分派实现](https://github.com/promptfoo/promptfoo/blob/main/src/assertions/index.ts) 与 [断言测试](https://github.com/promptfoo/promptfoo/blob/main/test/assertions/index.test.ts)。实现明确区分模型评分断言与其他断言，并有反向断言及基础类型测试。README 的本地执行不等于远程模型调用不联网；提供商与评分方式必须单独判断。

适配：优先检查模板加载、变量缺失、版本冻结、跨账号访问、冲突提示、TXT 格式和结构缺项。文学质量不可由字符串命中自动证明；结构检查只形成可查看报告，不触发无授权重试或直接宣称作品合格。

### Dramatron：层级一致性而非固定故事公式

已阅读 [README](https://github.com/google-deepmind/dramatron/blob/main/README.md)、[Colab 实现](https://github.com/google-deepmind/dramatron/blob/main/colab/dramatron.ipynb) 和许可证；通过完整默认分支树核查，未见独立测试路径，不能报告为“测试充分”。Notebook 包含标题、角色、场景等结构对象；README 也说明自上而下的生成顺序不适用于所有作者，并承认模型输出偏差等限制。

适配：模板写明每段的叙事目的与可见结果，风格规定节奏、对白、情绪、冲突与视觉语言。微电影、AI 剧情短片与短剧各有适合的推进方式，不强制所有内容使用同样三幕、三场或固定字数。控制角色、道具和空间连续性，同时停留在脚本／剧本层，不生成完整分镜表。

### 文件解析依赖：只提取可取得的文本

python-docx 的 [Document 实现](https://github.com/python-openxml/python-docx/blob/master/src/docx/document.py) 提供 `iter_inner_content()`，可按正文顺序访问段落和表格；[Document 测试](https://github.com/python-openxml/python-docx/blob/master/tests/test_document.py) 覆盖内容遍历、段落和表格访问。实现文档说明修订标记中的段落不会出现在普通段落列表中，顶层表格列表也不包含所有嵌套内容。不能把“成功打开”视为全文完整。

pypdf 的 [页面实现](https://github.com/py-pdf/pypdf/blob/main/pypdf/_page.py)、[文本提取测试](https://github.com/py-pdf/pypdf/blob/main/tests/test_text_extraction.py) 和 [官方提取说明](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md) 显示其文本及布局提取能力，也明确扫描图片可能只有很少或没有文本，需要另外的 OCR。当前契约不含 OCR，故应保留原件并显式提示无可提取文本，不将图片识别当成既有功能。

本地接入遵守契约的 TXT/DOCX/PDF 5 MiB、解析后 200000 字符限制。解析是本地读取，不获得执行文档指令、跨租户引用或调用模型的权限。上述限制由应用层校验，不能依赖第三方库默认值。

## 预设登记与可执行规则

`registry/templates.json` 保留旧 ID、旧路径与旧加载字段，补齐新版元信息。新增模板仍经 `template_file` 载入 UTF-8 TXT，不将结构写死在业务条件分支。

| 模板 | 类型 | 核心结构差异 |
| --- | --- | --- |
| 视频脚本基础结构（旧） | script | 主题、创意、可见事件、对应声音与结尾 |
| 分场短剧剧本（旧） | screenplay | 场次、人物、可见动作与对白，延续旧来源说明 |
| 商业广告 | script | 单一利益、使用情境、可信证明、行动收束 |
| 品牌宣传 | script | 品牌命题、人物行动、事实支撑、价值回响 |
| 产品介绍 | script | 用户任务、操作顺序、功能收益与适用边界 |
| 自媒体短视频 | script | 开头收益、例子兑现、结果与相关互动 |
| AI剧情短片 | screenplay | 单一主行动、稳定设定、因果转折、关键选择 |
| AI短剧 | screenplay | 眼前矛盾、策略对抗、线索反转、阶段回报 |
| 微电影 | screenplay | 人物缺口、尝试代价、关键选择、结尾余韵 |
| 纪录片 | script | 纪实问题、现场观察、可核实资料、开放结论 |
| 人物故事短片 | script | 当前行动、具体目标、经历取舍、可见变化 |
| 分镜友好脚本 | script | 明确动作主体、空间与道具状态、对应声音；不逐镜拆分 |

每项具备 `version:1`、`structure:[{section,purpose}]`、`required_fields`、`optional_fields`、`output_format`、`duration_rules`、`examples` 和 `keywords`。`duration_rules` 为 `min_seconds/max_seconds/guidance` 对象；建议区间只用于密度判断，用户给定时长优先，超出区间报告确认项，不自动改写需求。原有两项不设置推定时长上下限。

`registry/styles.json` 提供电影叙事、现实主义、青春治愈、喜剧、悬疑反转、科幻未来、商业广告、纪录片、诗意抒情、短剧高冲突、温情家庭、都市情感、快节奏短视频、极简叙事、实验视觉叙事 15 项。每项分别写明节奏、对白、情绪、冲突、视觉语言、人物塑造，以及可检查的约束、指导、原创例子和关键词。附加 `traits.pacing` 与 `traits.conflict` 作为冲突检测信号，具体叙事内容仍来自数据，不在后端按中文名称硬编码。

推荐依据用户已输入的类型与关键词给出理由，不能把规则匹配分数称作审美评分或真实模型概率。风格组合如“快节奏”与“缓慢留白”出现张力时，保留用户意图并要求确认；用户确认可形成有意识的混合表达，不静默替换为某一风格。风格不能覆盖纪实事实边界、用户明确禁用内容或脚本／剧本的输出层级。

## 复核与追踪

公共查询使用 `GET /repos/{owner}/{repo}`、`GET /repos/{owner}/{repo}/commits?per_page=1`、`GET /repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1`，并读取上列官方源文件。许可证判断以正文为准，不依赖 API 的自动识别标签。提交标识用于说明研究时点，链接默认分支便于后续维护者复查；将来变更研究结论时应更新日期和证据。

本轮已完成本地验证：两份 JSON 可解析；12 个模板 ID 与 15 个风格 ID 唯一；新旧元字段完整且版本为整数 `1`；必填字段仅 `user_request`；每个模板文件在对应 `templates/scripts/` 或 `templates/screenplays/` 内、可按 UTF-8 非空读取且未超过加载大小限制；现有 `ScriptTemplateLoader.load()` 已逐个载入全部 12 项，`list_styles()` 已读取全部 15 项。旧模板 ID、文件路径与原始 TXT 保留。应用层权限、快照、推荐及冲突检查的集成验收由主开发任务执行，本研究不将其描述为已实现结果。
