# Script Template & Style Engine 与个人模板交付报告

日期：2026-10-20。范围：现有脚本／剧本功能的本地增量开发。模板、风格、案例三层独立，并支持从既有作品、上传作品或手动草稿创建私有个人模板。沿用现有 Codex Adapter、模型选择、积分账本、TXT Artifact 和 Version，不创建第二套生成器。

## 1. 交付能力与数量

系统模板新增 **10 个**，原有 2 个 ID、路径和正文保留，共 **12 个**。新增商业广告、品牌宣传、产品介绍、自媒体短视频、AI 剧情短片、AI 短剧、微电影、纪录片、人物故事短片、分镜友好脚本。最后一项输出便于下游拆解的脚本，不执行分镜生成。

独立风格共 **15 个**：电影叙事、现实主义、青春治愈、喜剧、悬疑反转、科幻未来、商业广告、纪录片、诗意抒情、短剧高冲突、温情家庭、都市情感、快节奏短视频、极简叙事、实验视觉叙事。每项分别定义节奏、对白、情绪、冲突、视觉、人物与可执行约束。预设为本项目编写，不以在世创作者模仿作为默认。

个人模板可预览、修改结构和默认变量、复制、停用、查看历史版本；既有作品详情提供“保存为我的模板”。草稿确认后才产生正式模板。原始作品不覆盖、不公开，个人模板不依赖原作品绝对路径。

## 2. 开源研究与授权

已核实官方 README、实现、示例或测试和许可证，详见 [研究报告](research/script-template-research.md)，其中记录具体源文件、维护证据及适用限制。

| 项目 | 许可证检查 | 借鉴／采用 |
| --- | --- | --- |
| [LangChain](https://github.com/langchain-ai/langchain) | MIT | 变量与模板分离、显式校验；未安装框架 |
| [Langfuse](https://github.com/langfuse/langfuse) | 普通区域 MIT，指定企业目录另有 Enterprise License | 固定整数版本与快照；未复制企业代码或部署服务 |
| [Promptfoo](https://github.com/promptfoo/promptfoo) | MIT | 确定性断言与模型评分分离；未安装评估 CLI |
| [Dramatron](https://github.com/google-deepmind/dramatron) | Apache-2.0 | 人物目标、场景、动作与因果层次；未导入 Notebook 或 Prompt |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT | 本地解析 DOCX 段落和表格，采用已有依赖并声明版本范围 |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD 三条款，按许可证正文核实 | 本地提取带文字层 PDF，采用已有依赖并声明版本范围 |

没有复制大量第三方代码、Prompt 或作品，没有为研究新增模型服务。外部项目的测试只阅读未执行；Dramatron 未发现独立自动化测试目录，研究报告已注明。若分发解析依赖，应保留其版权及许可证。

## 3. 数据结构

完整边界以 [Schema 与 HTTP 契约](script_creative_contract.md) 为准。

| 资源 | 主要字段 |
| --- | --- |
| 系统 Template | `template_id, name, description, type, structure:[{section,purpose}], required_fields, optional_fields, output_format, duration_rules, examples, version, enabled` |
| Style | `style_id, name, description, narrative_pacing, dialogue_style, emotional_tone, conflict_style, visual_language, characterization, constraints, prompt_guidance, examples, version, enabled` |
| 私有 Example | `example_id, version, owner_account_id, workspace_id, main_project_id, subproject_id, filename, sha256, text, reference_roles, analysis, analysis_confirmed, enabled` |
| 个人 Template | `template_id, version, owner_account_id, workspace_id, name, description, type, structure, required_fields, optional_fields, variables:[{name,label,default}], style_guidance, format_rules, prompt_guidance, visibility, enabled, created_at, updated_at` |
| 来源追踪 | `source_type, source_artifact_id, source_version, source_example_id, extraction_method, analysis_model`，服务器确定来源，不接受客户端替换所有者或来源 |

私有资源位于现有 StorageManager 数据根下 `.workbench/script-resources/<account>/<workspace>/`；上传原件与 `vN.json` 版本记录均保留。`latest.json` 只指向最新版本，编辑不覆盖 `vN.json`，停用不删除旧内容。写操作有锁和版本冲突校验。系统配置不可通过个人模板写接口修改。

## 4. 上传、分析与模板提取

TXT、DOCX、PDF 上传限制为 5 MiB，提取文本最多 200000 字符。TXT 必须可按 UTF-8 读取；DOCX 限制压缩包展开体积、条目数量和实体声明；PDF 最多 100 页、不支持加密或无文字层扫描件。原件保留并校验 SHA-256。解析不还原 Word 完整版式，不进行 OCR，不把未提取到的内容补造成正文。

案例可分别选择结构、风格、对白、格式或综合用途；一个任务可引用至多 6 个私有案例，案例停用后不再接受新引用，既有任务保持快照。每次读取、下载、分析、更新和生成引用均校验账号与 Workspace，上传同时校验项目／子项目归属。

`RuleExampleAnalyzer` 识别栏目、场次、人物标记与段落组织，输出严格 `Analysis` Schema，无法可靠判断的情绪、冲突等明确要求人工确认。分析最多处理前 12000 字符，长文结果明确提示截取。用户可修改 JSON 后确认，确认会创建案例新版本。

个人模板直接结构提取优先保留已识别栏目、场次数或段落数，再提炼通用段落目的及可替换变量；无法识别时提供注明限制的基础草稿。AI 模式通过同一 Analyzer 接口运行，当前默认仍是免费规则预览。Mock 可以注入结构化输出或失败，用于验证 Schema、确认和失败保留原件。草稿规则包含连续来源正文时会拒绝直接保存，避免将整段故事变成通用规则；这属于基本拷贝检查，不是语义抄袭判定。

`existing_artifact` 记录原 Artifact ID 和作品版本，`uploaded_file` 记录 Example ID 和案例版本，`manual` 无作品依赖。来源固定在服务端草稿中，正式模板包含独立的提炼内容。分析失败不会产生正式模板，用户仍可手动创建。

## 5. 推荐与 Prompt Compiler

推荐先按 script/screenplay 过滤模板，再统计不同关键词命中。`match_score = min(命中关键词数, 4) / 4`，它是启发式覆盖分，**不是校准概率**。返回推荐模板、风格、理由、其他候选和缺失信息，不会自动应用或自动生成。用户可接受、改选、不选模板、不选风格或自行上传案例。

当前个人模板没有独立关键词管理，参与类型兼容候选但不进行语义排名。DeepSeek Flash 在现有配置中禁用，因此本轮没有语义兜底请求，也不以规则得分冒充 AI 判断。

Compiler 将需求、模板、风格、案例、父版本分别放入命名数据块。模板控制结构，风格控制表达，案例提供特征和实际摘录；用户明确约束优先。变量支持默认值及本次覆盖，必填变量缺失时在预留积分前拒绝。每个案例正文最多 1800 字符，总正文预算 12000 字符；截取保留实际原文并注明，已确认分析另作为结构化参考。

快／慢节奏、高／低冲突、明确用户要求与所选风格矛盾、时长超出模板建议区间，会返回冲突项。用户修改或明确确认之后才能提交。当前冲突检测基于规则，不声称穷尽所有语义矛盾。

引用内容经过 JSON 编码，明确作为数据，不执行文档中的命令。继续使用已有禁用外部工具的文本 Codex Adapter；没有新增 Shell、插件或外部 API 执行入口。

## 6. 任务、版本、积分与质量检查

报价绑定实际资源版本与内容 hash；资源更新导致旧报价失效。提交时冻结 `creative_snapshot` 和编译后的 Prompt，公开元数据与 Version 保存 `resource_references`。后续资源修改、停用或系统配置升级不改变已排队任务的生成输入和历史来源。

模型选择、思考强度、固定价格、积分预留／结算／失败释放继续使用现有实现。`config/script_models.json`、`script_pricing.json`、`script_routing.json` 与开发基线逐字节一致，详见验收 manifest。未新增分析扣费项目，未重写 Codex 调用。

生成仍保存 UTF-8 TXT，经原有非空、编码及非成品封装校验，再登记 Artifact／Version。新增基础报告检查结构栏目、必要标记、段落和明显截断疑点，显示文件存在、Artifact 与 Version 登记结果。结构建议不触发模型重试，不评文学质量。

本地浏览器复测发现历史扫描偶尔读取原子写入 `.tmp-*.json` 的既有并发边界；脚本任务扫描已统一忽略这种临时文件，覆盖历史列表、待执行恢复和下游依赖扫描，并新增确定性回归。

## 7. UI

仅扩展脚本页与作品查看区域：系统／我的模板分组、自由生成、15 风格多选、案例上传与用途勾选、分析预览与确认、推荐预览、模板变量、冲突确认、保存为我的模板、管理与历史。原有模型、思考强度、积分和下载保留。

个人模板与案例分析暂用结构化 JSON 编辑器，提供预览和保存确认。这是已可用的最小界面；是否将编辑器改成逐项表单，可由用户决定下一轮设计，不阻塞当前后端能力。

本地浏览器使用独立测试账号、独立数据目录与固定文本 `PreviewAdapter`。已走通“既有作品→结构草稿→确认个人模板→编辑为 v2→选择模板／风格／案例→手动 Sol High→20 测试积分报价→Mock TXT→Artifact／Version”。截图与生成来源证据位于 `reports/script-creative-engine/`，截图不代表真实模型创作能力。

## 8. 文件清单

| 新增 | 用途 |
| --- | --- |
| `image_workbench/script_resources.py` | 私有资源 Schema、上传解析、Analyzer、草稿／版本管理 |
| `image_workbench/script_engine.py` | 资源解析、推荐、冲突、快照、基础检查 |
| `image_workbench/script_resource_web.py` | 创作资源 HTTP 接口 |
| `skills/video-script-generation/registry/styles.json` | 15 风格配置 |
| `templates/scripts/{commercial_ad,brand_promo,product_intro,social_short_video,documentary,character_story,storyboard_friendly}.txt` | 7 个脚本模板，路径位于上述 Skill 内 |
| `templates/screenplays/{ai_story_short,ai_short_drama,microfilm}.txt` | 3 个剧本模板，路径位于上述 Skill 内 |
| `tests/test_script_creative_core.py`、`test_script_creative_http.py` | 核心与 HTTP 的离线验收 |
| `docs/script_creative_contract.md`、本文、`docs/research/script-template-research.md` | 契约、交付、研究 |

修改：`script_templates.py`（独立风格与 Compiler）、`script_service.py`（资源装配、报价／冻结、版本来源、结构报告、任务扫描）、`contracts/scripts.py`（兼容新增参数）、`script_web.py`（安装资源接口）、`web_static/scripts.js`、`scripts.css`、`web_templates/scripts.html`、`registry/templates.json`、Skill／宿主接口／原脚本文档、`requirements-web.txt`、`tests/test_script_templates.py`、`scripts/verify.py`。项目并行重构中的 `architecture.json` 已声明新模块归属，通过边界检查。

现有同目录中的分镜及架构重构改动按当前代码保留，不把它们记作本功能新建模块。集成前 8 个重叠文件备份在 `reports/script-creative-engine/pre-integration/`；隔离验收夹具在 `work/script-creative-dev/`。没有整体覆盖其他聊天改动，没有提交整个尚未建立 Git 基线的目录。

## 9. 测试与证据

必需检查命令：`.venv\Scripts\python.exe -X utf8 scripts\verify.py --module scripts`，包含新创作测试、原脚本测试、公共 HTTP／请求边界／前端／架构与视频流程兼容检查。最新结果以 [模块日志](../reports/script-creative-engine/module-verification.log) 和 [验收 manifest](../reports/script-creative-engine/acceptance.json) 为准。

覆盖：12×15 组合、TXT/DOCX/PDF、非法／空／超限文件、账号及 Workspace 隔离、规则推荐、Mock 分析成功／失败、手动覆盖、自由生成、Prompt 保留约束、个人模板三种来源、预览确认、不可伪造来源、复制停用、编辑冲突、历史冻结、UTF-8 TXT、Artifact／Version、原模型和价格、幂等与失败退款。

最终项目模块检查 **154 项：153 通过、1 项环境跳过**；分镜兼容检查 **56 项全部通过**。架构检查为 75 个模块、377 处导入、0 违规。模块检查开始和结束的源码指纹一致。唯一跳过为 Windows 本机无法创建真实符号链接的环境分支，相关路径及重解析边界另有验证，不删测试或降低门槛。

首次合并时脚本 125 项、分镜 54 项通过；修复临时文件扫描后，一次检查期间其他聊天新增了分镜恢复测试，验证工具按设计返回“源码已变化”，没有将该次结果用作最终验收。保留该次记录为 `module-concurrent-edit.log`；重新执行后的最终结果如上。

全程使用 Mock 或固定预览适配器，没有真实付费模型调用。未执行生产发布检查，不把这些结果宣称为已上线或生产端到端验收。公共前端语法检查随模块测试执行。

## 10. 未启用能力与审批

真实语义 Example Analyzer／AI 模板提取尚未接入可用服务，当前明确使用免费规则；其结构化接口和 Mock 已验证。推荐没有真实语义评分，PDF 没有 OCR，结构报告没有文学评分。系统模板／风格按配置管理，个人资源按 UI 管理；没有公共模板发布或跨账号分享功能。

本轮没有 `git push`、部署、服务器上传、生产配置修改、生产数据库迁移或真实模型推理。下一步上线需用户明确批准，再基于届时合并完成的代码执行发布前全量检查。若后续启用真实分析模型，需要另行确定提供商、实际费用规则和用户触发方式；本轮不添加任何收费项。
