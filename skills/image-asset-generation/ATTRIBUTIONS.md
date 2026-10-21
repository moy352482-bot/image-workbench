# 方法来源与许可记录

核验日：**2026-10-16**。四个仓库均仅用于本轮方法研究：**未复制任何源码、Prompt、Preview、参考图片、数据集或私人库**。本 Skill 的流程文字、结构化定义、Style 和示例独立编写；技术字段名称和来源链接用于说明接口及研究依据。

此文件是来源记录，不是为本项目或本 Skill 新增 MIT 或其他整体许可证。表中许可仅描述所列上游对象；公开可读、署名或根目录许可证不能代替第三方内容的授权。

## 固定研究版本

| 仓库 | commit | commit 日期 |
|---|---|---|
| [VigoZhao/AI-Visual-Prompt-Cookbook][cook-readme] | `f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d` | 2026-10-15 |
| [wuyoscar/GPT-Image2-Skill][gpt-readme] | `9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f` | 2026-09-30 |
| [jau123/MeiGen-AI-Design-MCP][mei-readme] | `847fbd995a6acb18a6d05ba33f3357451394340f` | 2026-10-15 |
| [zsky-ai/zsky-prompt-library][z-readme] | `151b8aa1f10d9638752063cb5491df22967ba359` | 2026-05-23 |

## 按内容对象核验

| 来源 / 对象 | 许可和署名、商用依据 | 未核实项与本版处理 |
|---|---|---|
| Cookbook：代码、脚本、文档、目录结构 | README 声明代码/脚本/文档为 MIT；根 LICENSE 的对象措辞限定为 repository structure and associated documentation files，版权 `2026 VigoZhao`。MIT 允许商业使用有权许可的内容；复制或实质复用需保留版权及许可通知。[证据][cook-license] | 不把此 MIT 扩展到 Style 内容和图片；仅研究组织方式。 |
| Cookbook：`style.json` Prompt 内容 | README 单列 **CC BY 4.0**，要求 credit `@VigoCreativeAI`。该许可允许商用和改编；复用应注明作者、来源、许可链接及改动，保留所附通知，不对许可权利增加限制。[仓库声明][cook-readme]、[CC BY 4.0 条款][ccby] | 本版未改写或收录上游 Prompt，不构成现成风格库移植。 |
| Cookbook：Preview / 数据 | README 仅将 Preview 定义为 visual references，未给出图片再发布或商用许可。未发现逐图独立授权；结构化 Prompt 数据遵循上述内容许可，不能由代码 MIT 推断。 | 图片及可能涉及的品牌/参考素材权利未逐项核实；不复制、不嵌入。 |
| GPT：软件和关联文档 | 根 MIT，版权 `2026 Wuyoscar`，商业使用及复制条件如许可文本。[证据][gpt-license] | 仅研究流程；未复制实现。 |
| GPT：图库 Prompt / 社区数据 | 内部资料分 `Curated` 与外部作者来源；贡献规范要求外部内容保留 Author + Source。社区 JSON 有 Reddit/小红书来源、摘录与改写，但没有逐项 license 字段。[贡献规范][gpt-contributing]、[社区数据][gpt-community] | 来源署名不是第三方商用或再分发授权。各条权利未全部核实，不收录 Prompt/数据。 |
| GPT：图片 / Preview | 图库包含生成样例、改编及外部来源；部分标记 Curated/Original，部分附来源。未建立逐图独立许可和参考素材授权链。 | 根 MIT 不证明所有人物、商标、输入图片已获授权；不复制 Preview。具体第三方例外见下表。 |
| MeiGen：软件和关联文档 | 根 MIT，版权 `2026 MeiGen`；复制或实质复用需保留通知，许可允许商用其有权许可的对象。[证据][mei-license] | 仅研究业务契约和编排边界，未复制代码。 |
| MeiGen：内置 Prompt | `src/lib/prompts.ts` 标注来源 `edgeone/vision-api/node-functions/api/[[default]].js`，该处未给可追溯上游 URL 或独立授权。[证据][mei-prompts] | 这段来源的完整许可链未核实；不复制其 system prompt。 |
| MeiGen：公开/私人 Prompt 数据 | 在线搜索结果有作者、Prompt、图片等，没有逐项 license 字段。3.0 已删除离线库，私人库仅远程 OAuth 可访问。[schema][mei-search]、[迁移说明][mei-migration] | 未连接服务、未读取私人库。平台内容不能因为客户端 MIT 而被视为开放数据。 |
| MeiGen：图片 / Preview / 服务结果 | Git 中 `assets/*.jpg` 未见逐图独立授权明细。官网条款将图库定义为第三方公开作品归档，并区分用户自己的生成结果商用权与社区投稿授予平台的权利。[服务条款][mei-terms]（2026-10-12 生效，核验日在线内容，非固定 Git 版本） | 条款不等于第三方图库对所有访问者开放再分发授权；不复制 assets、图库图片或数据。 |
| ZSky：文档与 Prompt | 根 MIT，版权 `2026 ZSky AI`；README 明确 Prompt 可修改、分发和商用。复制或实质复用需保留 MIT 版权/许可通知。[LICENSE][z-license]、[README][z-readme] | 本版仍选择原创，仅学习摄影维度拆分方法。 |
| ZSky：图片 / 外部数据 | 固定树未包含图片文件；未发现单独数据许可证。外链服务和素材不因本仓库 MIT 自动获得许可。 | 没有复用外链内容，其独立授权未核实。 |

## GPT 图库中的第三方许可链样本

以下核验说明为什么不能以聚合仓库的根 MIT 覆盖全部内容；不是对整个图库的逐项清权结论。

| 被引用来源 | 固定证据 | 判断 |
|---|---|---|
| `LunarXuan/natural-sns-portrait` | commit `5c1a62c8e2335ba635f4f2b6cfa766cb36a82575`（2026-08-26），[README 许可段][lunar] 明确未附独立许可证；文件树无 LICENSE。GPT README 对自然人像示例链接此仓库。 | 未确认公开再分发或商用许可；不复制。 |
| `openai/openai-cookbook` | commit `5bd6cb40b6b84b69b2032d810610aa85064102df`（2026-10-09）的 [LICENSE][openai-license] 为 MIT，版权 `2025 OpenAI`；GPT 的 [参考文件][gpt-cookbook] 标注原来源。 | 若实际复用其许可范围内内容，需保存 OpenAI 版权/许可；本版未复用。图片或另有来源的内容仍需独立确认。 |
| `EvoLinkAI/awesome-gpt-image-2-prompts` | commit `e2a269ad1a055a0b4f6c1e170341c6c1aba30faa`（2026-07-18）的 [LICENSE][evo-license] 为 CC0-1.0。 | 对提供者拥有的权利允许包括商用在内的使用，不要求署名；不消除第三方权利或商标权。不能由聚合方 CC0 推断所有转载内容已授权；本版未复用。 |
| Reddit / X / 小红书来源 | GPT [社区索引][gpt-community] 及 [2.5 模板来源][gpt-templates] 保留外链和部分作者信息。 | 未逐条确认作者另行授权；公开发布和给出出处不足以确认商用/再分发许可。全部不收录。 |

后续确需引入第三方内容时，应逐项记录原作者、固定来源、具体复制对象、适用许可、是否改动及必要通知；没有明确许可的图片、私人 Prompt 和品牌素材不得由此研究记录推定已获授权。

[cook-readme]: https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook/blob/f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d/README.md#license
[cook-license]: https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook/blob/f3f1da31e8c55d5e4c4b1b3e1bbc3ebec639f33d/LICENSE
[ccby]: https://creativecommons.org/licenses/by/4.0/
[gpt-readme]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/README.md
[gpt-license]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/LICENSE
[gpt-contributing]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/CONTRIBUTING.md
[gpt-community]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/docs/community-prompt-picks.json
[gpt-cookbook]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/skills/gpt-image/references/openai-cookbook.md
[gpt-templates]: https://github.com/wuyoscar/GPT-Image2-Skill/blob/9f8aa1a44a4a5c0f5a5d6e45eed564b3e2d5b87f/skills/gpt-image/references/templates-gpt-image-2.5.md
[mei-readme]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/README.md
[mei-license]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/LICENSE
[mei-prompts]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/src/lib/prompts.ts
[mei-search]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/src/lib/api-search.ts
[mei-migration]: https://github.com/jau123/MeiGen-AI-Design-MCP/blob/847fbd995a6acb18a6d05ba33f3357451394340f/RELEASE_NOTES_3.0.0.md
[mei-terms]: https://www.meigen.ai/terms
[z-readme]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/README.md#license
[z-license]: https://github.com/zsky-ai/zsky-prompt-library/blob/151b8aa1f10d9638752063cb5491df22967ba359/LICENSE
[lunar]: https://github.com/LunarXuan/natural-sns-portrait/blob/5c1a62c8e2335ba635f4f2b6cfa766cb36a82575/README.md
[openai-license]: https://github.com/openai/openai-cookbook/blob/5bd6cb40b6b84b69b2032d810610aa85064102df/LICENSE
[evo-license]: https://github.com/EvoLinkAI/awesome-gpt-image-2-prompts/blob/e2a269ad1a055a0b4f6c1e170341c6c1aba30faa/LICENSE
