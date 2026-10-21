# Image Asset Generation Skill v1 交付报告

日期：2026-10-16。本轮交付是可供 Agent 使用的规划 Skill、结构化契约、原创 Style 与离线验证包；没有连接真实生成服务。入口：[SKILL.md](../SKILL.md)。

## 1. 当前项目原有结构

业务代码在 `image_workbench/`，原有验证和运维资料分别在 `tests/`、`scripts/`、`docs/`、`deploy/`；物理图片由配置的数据根目录管理。项目没有既有的项目级 Skill 加载机制，因此新增 `skills/image-asset-generation/`，没有把依赖目录中的 Skill 当作项目规范，也没有安装全局副本。

已核查 MainProject/Subproject/TaskPlacement、账号与工作空间隔离、TaskRunner/Worker、StorageManager、Artifact、ImageBackend、远端/本地调用链、日志和积分。现有 Artifact 是不可变文件记录，编辑通过来源 Artifact 表示血缘；没有独立通用 AssetVersion 类或资产批准指针。详见 [项目集成契约](project-integration.md)。

## 2. 研究的开源项目

四个指定仓库都固定到具体 commit 检查，包含 README、许可、实际数据结构及相关源码。固定版本、文件证据和链接在 [研究记录](research.md)。

- [AI Visual Prompt Cookbook](https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook)：结构化 Style、模板变量、风格锚点、示例、Schema 与验证。
- [GPT-Image2-Skill](https://github.com/wuyoscar/GPT-Image2-Skill)：请求分类、模型解析、参考处理、执行说明、输出和错误处理。
- [MeiGen-AI-Design-MCP](https://github.com/jau123/MeiGen-AI-Design-MCP)：创意 Skill、业务工具、provider 接口、参考角色、编排恢复和当前远程图库。
- [ZSky Prompt Library](https://github.com/zsky-ai/zsky-prompt-library)：摄影、光线、镜头、人像、负向约束及工具无关的组件组织。

## 3. 吸收的设计

| 来源 | 本项目采用的方法 |
|---|---|
| Cookbook | 每个 Style 独立数据文件，主体变量与稳定视觉锚点分离，用统一 Schema 与模板闭包校验 |
| GPT-Image2-Skill | 先分类请求，再按需读取参考；分开保持项与修改项；明确参数、结果和失败边界 |
| MeiGen | 工作意图与 provider 实现分层；参考有角色；步骤保留依赖；不确定结果先恢复原任务 |
| ZSky | 将构图、光线、颜色、材质、镜头、景深作为可组合视觉维度 |

## 4. 未采用的内容及原因

没有收录外部 Prompt、Preview、图库、品牌素材或私人库；没有复制上游实现。没有沿用固定模型菜单、凭证搜索链、第二套 provider 调用器或任务调度器，以免绕过本项目账号、积分、执行与归档边界。

研究还区分了声明与实现：Cookbook 部分数据与其 Schema 不一致；MeiGen 通用 adapter 的 edits/mask/negative 参数并非全部贯通，历史离线图库也已移除；聚合图库的署名不等于逐条授权。这些缺口没有被当作本项目可用能力。

## 5. License / Attribution

完整分层记录见 [ATTRIBUTIONS.md](../ATTRIBUTIONS.md)。Cookbook 的代码/文档 MIT 与 `style.json` 的 CC BY 4.0 分开，Preview 没有被当作可再分发素材。GPT 与 MeiGen 的根 MIT 没有被扩展到其第三方 Prompt、图片和远程数据；具体来源存在未明确许可或授权链不完整的情况。ZSky 明确允许其 Prompt 商用，但本版仍全部独立编写。

本版只借鉴方法，15 个 Style、30 个风格示例及测试请求为原创；没有为本项目擅自增加整体许可证，也没有以来源记录替代第三方授权。

## 6. 新增文件

全部交付文件位于 `skills/image-asset-generation/`：

```text
SKILL.md
ATTRIBUTIONS.md
schemas/                    11 份 JSON Schema
styles/index.json
styles/<15 个 slug>/style.json
references/
  project-integration.md    当前源码事实与未来集成边界
  research.md               四库研究与取舍
  asset-types.md            12 类资产字段说明
  consistency.md            固定属性、参考绑定与镜头覆盖
  style-system.md           风格选择与变量规则
  prompt-compilation.md     12 层编译与每步生成计划
  lifecycle-and-quality.md  生命周期、批准、成本和最低 QA
  test-spec.md              离线验证方法与限制
  delivery-report.md        本报告
examples/asset-generation-examples.json
tests/validate_skill.py
tests/requirements.txt
tests/reports/              验证、独立试用与原文件差异证据
```

验证依赖装在独立临时目录，没有改变应用依赖。详细文件清单见 [file-manifest.json](../tests/reports/file-manifest.json)。

## 7. AssetSpec Schema

[asset-spec.schema.json](../schemas/asset-spec.schema.json) 包含所需的身份、外观、服装、材质、形状、颜色、环境、构图、光线、镜头、Style、Reference、约束、比例、分辨率、生成策略、模型偏好、状态和版本；另有固定属性路径、镜头覆盖白名单、一致性绑定、假设与来源。

12 类资产都有独立必填条件。人物强制 character_id、年龄、脸、头发、皮肤、身材、衣着、饰物与一致性约束；产品强制身份、形状、材质、品牌、表面、logo 和尺寸描述。独立 [Character](../schemas/character.schema.json) 与 [Product](../schemas/product.schema.json) 入口复用主 Schema，避免两套字段定义漂移。

## 8. Style System Schema

[style.schema.json](../schemas/style.schema.json) 定义视觉定位、适用类型、六维视觉语言、core/adaptable 锚点、变量/枚举/默认值、模板、负向约束、来源排除项、示例和模型无关说明。索引只保存 ID 与路径，避免重复元数据。

Style 内容不固定主体或模型。变量闭包、示例取值、锚点唯一性与资产类型匹配均有离线校验；固定资产身份优先，风格不得改变产品颜色或人物特征。

## 9. 第一版 15 个 Style

cinematic-realism、commercial-product、editorial-fashion、documentary、anime-manga、illustration、watercolor、3d-stylized、retro-film、futuristic-sci-fi、minimal、graphic-poster、lifestyle、architectural、food-photography。

每个包含 3 个变量、4 个核心/适配锚点和 2 个不同使用示例，共 30 个例子。视觉定位与选择理由见 [Style System](style-system.md)；数据见 [索引](../styles/index.json)。

## 10. 完整执行流程

自然语言需求 → 资产分类及必要拆分 → AssetSpec → Visual Intent/Category/Style 选择 → 参考角色及固定身份校验 → 12 层 Prompt → 每步 Prompt/依赖/参数与资源需求 → 宿主实时报价 → 生成授权 → 原有执行链 → Artifact 归档及技术检查 → DRAFT/REVIEW → 具体产物人工批准 → 新批准版本。

本轮产出停在 PLANNED。未知价格为 null；预算 0 不变成一次授权；每步一个产物；没有自动重试或静默切换模型。生成授权与批准产物是两件事。重生成和修改都派生新版本，旧批准版在新版本获批前继续有效。

## 11. Skill 输入与输出 Schema

[输入](../schemas/skill-input.schema.json) 最小只需 user_request，也支持资产类型、参考、自然语言风格、比例、质量、预算、操作意图、既有版本及 scene。缺省值由 Agent 明确归一化。

[输出](../schemas/skill-output.schema.json) 必须包含 AssetSpec、选中 Style 与理由、最终 Prompt、12 层内容、参考策略、GenerationPlan、版本、空 artifact_ids 与待报价成本。[GenerationPlan](../schemas/generation-plan.schema.json) 记录逐步 Prompt、依赖、引用角色、请求/实际可映射参数、预算和 blocker。

此输出 Schema 刻意只表达规划；未来执行回执和资产版本元数据由 [AssetVersion](../schemas/asset-version.schema.json) 与 [QualityCheck](../schemas/quality-check.schema.json) 契约描述，不能把规划对象冒充真实成功回执。

## 12. 与现有核心系统的接口关系

```text
Image Asset Skill（本轮：规划）
    ↓ 未来经认证 Host Adapter / 能力解析 / 报价与确认
现有任务提交入口 → Worker / TaskRunner → ImageBackend
    ↓
StorageManager → 不可变 Artifact → Physical File
    ↑
Asset → AssetVersion 元数据 → artifact_ids[]
            └→ 人工批准选择（未来注册器）
```

由于当前不存在独立 Version 注册器，采用 `Asset → AssetVersion → Artifact → Physical File` 更符合源码，避免把 Artifact 当作可反复覆盖的逻辑资产。新层只关联现有文件，不另建存储系统。执行适配器必须受当前账号/工作空间约束，复用积分与幂等；远端已经生成的结果只归档，不重复生图。

当前入口只支持 generate/edit；比例枚举限 auto、1:1、3:2、2:3，且只是 Prompt 指导。16:9、精确尺寸、原生 inpaint/mask 等缺口在计划中明确保留并阻止提交，不能靠修改措辞宣称能力已实现。

## 13. 自测结果

[离线验证报告](../tests/reports/validation.json)：11 份 Schema、15 个 Style、30 个风格例子、24 个完整请求输出案例全部通过。24 例覆盖 12 类资产和全部 15 种风格；另有 9 个补充合成基底、3 个技术检查合同、4 个版本合同。62 个错误变体全部被拒绝，未登记的远程 Schema 也被拒绝加载。

[独立试用报告](../tests/reports/forward-evaluation.json)：另一 Agent 从新请求独立完成 3 份计划，主 Agent 再次通过 Schema 与语义检查。试用促成了每步 Prompt/参考角色、零预算语义及缺失既有身份的表达修正；三例均保持 PLANNED，真实执行受明确 blocker 限制。Skill 标准入口检查也通过。

这些结果验证结构、引用、约束、预算及所列错误的拦截，不证明任意自然语言理解、真实图片一致性、审美或生产登记。图片模型调用和积分操作均为 0。

[原文件差异记录](../tests/reports/core-preservation.json)：检查任务前保存的 130 个文件快照，最后核对时其中 124 个未变，6 个视频模块/脚本/测试文件同期发生变化；本轮及委派写入均限于本 Skill，未修改或回滚这些视频文件。图片 Backend、TaskRunner、StorageManager 及积分核心没有出现在差异中。不能据此声称整个工作区从未变化。

## 14. 当前未实现部分

尚未实现生产 Asset Registry/AssetVersion 持久化与批准指针、宿主加载器/认证适配器、真实模型能力路由与 Model Adapter、可执行生产 Prompt Compiler、计划与实时报价/授权快照绑定、真实 AssetQualityCheck 编排，以及网页集成。

没有做真实图片验证、视觉评分、自动选最佳图，也没有编纂或扩展 Video/Storyboard/Shot Skill。这些都是本轮明确的范围边界；已完成的文件可作为后续集成的规格与离线验收依据。
