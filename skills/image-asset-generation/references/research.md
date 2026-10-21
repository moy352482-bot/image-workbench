# Image Asset Generation Skill：研究依据

核验日：2026-10-16。研究方式为公开文档和固定版本源码只读检查；没有运行上游项目、安装依赖、接入 provider、访问私人库或调用生图。以下均是方法参考，未复制任何源码、Prompt 或 Preview。许可分层见 [ATTRIBUTIONS.md](../ATTRIBUTIONS.md)。

| 项目 | 固定 commit | commit 日期 |
|---|---|---|
| AI Visual Prompt Cookbook | `f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d` | 2026-10-15 |
| GPT-Image2-Skill | `9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f` | 2026-09-30 |
| MeiGen-AI-Design-MCP | `847fbd995a6acb18a6d05ba33f3357451394340f` | 2026-10-15 |
| ZSky Prompt Library | `151b8aa1f10d9638752063cb5491df22967ba359` | 2026-05-23 |

## 1. AI Visual Prompt Cookbook：把风格变成数据

`styles/<slug>/style.json` 将 `environment_variables`、`prompt_template`、`examples`、`style_fidelity_anchors`、`source_content_to_avoid`、`negative_prompt` 分开；案例提供变量值，锚点约束风格稳定性，排除项防止参考内容混入新主题。v2.2 按风格需要选变量，不要求每种风格机械填写所有视觉维度。[目录与格式][cook-readme]、[实际 Style][cook-style]

吸收：每个风格有稳定 ID、版本、适用资产类型、变量定义、组合规则、锚点和原创示例；把可替换主体与应保留的视觉规律分开。不采用原始风格文本、Preview、品牌及现成主题，也不直接搬用其 schema。

实际缺口：当前 `pixel-weave-editorial` 的 `image_treatment` 是字符串，而 schema 要对象；`visual_deconstruction` 仅两项，schema 最少三项。CI 调用自定义 validator，不能据此断言全部数据通过 JSON Schema。我们采用一个权威 schema 校验全部风格与示例，再检查模板变量引用。[schema][cook-schema]、[validator][cook-validator]、[CI][cook-ci]

## 2. GPT-Image2-Skill：让 Agent 知道如何执行

其流程先分类 `generate / edit / inpaint / multi-reference`，识别文字、比例、参考和预算，再解析模型、准备输入、执行、报告。`gallery.md` 路由分类资料；`models.md` 与模型特定参考分离，精确请求不必加载整库。吸收这种渐进读取，以及“编辑目标”和“必须保持项”分开表达；模型和 API 参数不写进视觉 Prompt。[Skill][gpt-skill]、[图库索引][gpt-gallery]、[模型说明][gpt-models]

源码确有 generations/edits 路由、多参考数组、mask、参数检查、禁用 SDK 自动重试、空结果报错及输出文件路径。四类任务意图并不等于四个独立接口。仅检查文件存在不能保证 mask 格式有效；返回成功也不能证明人物一致、文字正确。[CLI][gpt-cli]

不采用其固定模型菜单、凭证搜索链和 CLI 强绑定。文档自述的 live 验证只覆盖部分模型/操作，离线请求测试不是本项目生图验证。我们的模型偏好只是计划输入；执行必须满足当前 Backend 能力，禁止静默降级、换模型或重复付费。

## 3. MeiGen：工作意图与执行能力分层

可选的 `plugin/skills/*/SKILL.md` 描述创意流程；`src/tools/skills.ts` 则定义抠图、海报、产品详情、背景和放大等云端业务输入。产品、logo、风格、补充角度参考各有职责；样式参考不得带入原产品与文案。吸收参考角色、独立模块和调用者拥有创意计划的边界。[创意流程][mei-skill]、[业务输入][mei-tools]

通用生成有 provider 接口，显式 provider 不可用会报错；但五个业务工具只适用 MeiGen Cloud。OpenAI-compatible adapter 只调用 generations，没有标准 multipart edits/mask；schema 的 `negativePrompt` 在当前通用处理链未传给 adapter。不能把接口名字当成已贯通的能力。[接口][mei-types]、[adapter][mei-openai]、[调用链][mei-generate]

编排保存每步 `requestId`、原参数和依赖；中断先查原任务，`unknown` 不意味着失败。四个本地槽限制提交，不限制远端在途任务总量，也不构成原子预算。我们只借鉴状态和恢复约束，调度、额度及重试仍归现有 TaskRunner。[编排契约][mei-workflow]

历史离线约 1,400 条 Prompt 库已在 3.0 移除；当前公开检索依赖在线服务，私人库依赖远程 OAuth。云端内部 Prompt/生成实现不在这份 client 源码中，不复制其库，也不接入第二套服务。[迁移说明][mei-migration]

## 4. ZSky：把摄影知识拆成组件

分类文件分别组织灯光、机位、人像、棚景和负面约束；贡献规则要求 tool-agnostic。吸收摄影维度可独立选择、再按任务组合的方式；将画面描述与模型专有参数分离。[灯光][z-light]、[机位][z-camera]、[人像][z-portrait]、[棚景][z-studio]、[负面约束][z-negative]、[贡献规则][z-contributing]

未见机器 schema 或验证测试；维护者关于通用性、生产测试的陈述不是我们的实测。通用 negative 可能禁止任务需要的颗粒、logo 或硬阴影，不能整段默认拼接。我们原创组件，逐项消解与正向约束的冲突。

## 本项目的取舍

采用 `AssetSpec → Style/Reference 选择 → GenerationPlan → 现有执行链 → 视觉验收 → 结果记录`。AssetSpec 描述资产身份与目标，Style 描述视觉规律，模型能力决定可执行操作；不得互相替代。资产身份不随单次生成改变，失败与未确认结果保留原任务关联；风格版本、参考来源和生成参数共同形成可追溯记录。

本版是原创声明、数据和执行说明；不据此宣称已接入运行时、已完成视觉一致性实测或新增任何 Backend 能力。后续接入必须单独验证现有调用链的字段映射和真实产物。

[cook-readme]: https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook/blob/f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d/README.md
[cook-style]: https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook/blob/f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d/styles/pixel-weave-editorial/style.json
[cook-schema]: https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook/blob/f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d/schemas/style-v2.1.schema.json
[cook-validator]: https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook/blob/f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d/scripts/validate-style-json.py
[cook-ci]: https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook/blob/f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d/.github/workflows/validate.yml
[gpt-skill]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/skills/gpt-image/SKILL.md
[gpt-gallery]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/skills/gpt-image/references/gallery.md
[gpt-models]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/skills/gpt-image/references/models.md
[gpt-cli]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/src/gpt_image_cli/cli.py
[mei-skill]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/plugin/skills/visual-creative/SKILL.md
[mei-tools]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/src/tools/skills.ts
[mei-types]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/src/lib/providers/types.ts
[mei-openai]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/src/lib/providers/openai.ts
[mei-generate]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/src/tools/generate-image.ts
[mei-workflow]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/COMPOSABLE_WORKFLOWS.md
[mei-migration]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/RELEASE_NOTES_3.0.0.md
[z-light]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/categories/cinematic-lighting.md
[z-camera]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/categories/camera-angles.md
[z-portrait]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/categories/portraits.md
[z-studio]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/categories/studio-backgrounds.md
[z-negative]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/categories/negative-prompts.md
[z-contributing]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/CONTRIBUTING.md
