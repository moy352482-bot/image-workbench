# 固定身份、镜头变化与版本一致性

本文件是 Agent 的语义检查规则，配合 [AssetSpec Schema](../schemas/asset-spec.schema.json)、[Character Schema](../schemas/character.schema.json) 和 [共享定义](../schemas/common.schema.json) 使用。Schema 检查字段形状；跨字段相等、引用可解析和版本关系还需要语义检查。

## 身份与版本的不同含义

| 字段 | 含义 | 规则 |
| --- | --- | --- |
| `asset_id` | 稳定逻辑资产身份 | 如 `character_001`；不是工作台 Artifact ID |
| `identity.character_id` | Character 的同一逻辑身份 | Character 必须与 `asset_id` 完全相等，不能另造一个人物 ID |
| `schema_version` | AssetSpec 数据格式版本 | 当前为 `1.0.0`，不随人物修订递增 |
| `version` | 当前目标规格版本 | 使用 `v1`、`v2` 等；它尚不证明图片或生产版本存在 |
| `generation_strategy.base_version` | 本次再生、编辑或派生所基于的旧版本 | 与目标 `version` 不同；初次 create 可为 `null` |
| `consistency.canonical_version` | 身份一致性的基线版本 | 必须是可解释、可解析的同一资产版本；无可验证基线时为 `null` |

例如，以 `character_001:v3` 为已批准身份基线，计划修改服装：目标 `version` 为 `v4`，`base_version` 为 `v3`，`canonical_version` 仍可为 `v3`。这些值表达三种职责，不能全部机械写成目标 `v4`。

若采用新版本作为未来身份基线，必须明确记录选择依据并经用户批准；不要因为版本号较大便自动更换基线。`latest`、时间排序第一项或最新生成图片都不能替代固定引用。

## Character 固定资料

Character 至少明确 `identity.identity_description`、`identity.visual_signature`，以及 `appearance.age`、`face`、`hair`、`skin`、`body`、`accessories`。基础服装使用 `clothing.base`，服饰配件使用 `clothing.accessories`；连续性备注放在 `type_details.continuity_notes`。

年龄与外貌是用户给定或经说明的设定，不根据参考图猜测敏感身份。`visual_signature` 写可见且可区分的特征；避免只有“漂亮、精致、主角感”这类无法核对的形容词。

把必须延续的身份特征写入 `consistency.consistency_constraints`。需要保留脸型、发型、肤色、体型、服装时分别说清，不以“保持一致”替代具体条件。约束是生成意图，真实效果仍待人工查看。

## 四种参考绑定

`consistency.reference_bindings` 的四个键必须存在。值为 `null` 或 `references[]` 中一个唯一的 `reference_id`，不是文件路径、Artifact ID 或臆造的文件名。

| 绑定键 | 对应引用 `roles` 必须包含 | 用途 |
| --- | --- | --- |
| `primary_reference` | `primary_identity` | 核对整体身份和最具代表性的外观 |
| `face_reference` | `face` | 核对脸型、五官关系、发际线等 |
| `full_body_reference` | `full_body` | 核对体型比例、身高观感及整体轮廓 |
| `clothing_reference` | `clothing` | 核对基础服装、裁片、配件和配色 |

同一张图可以承担多种角色，但其 `roles` 必须明确列出这些角色。多张引用不得共享同一个 `reference_id`。脸部图不能只因存在就自动承担全身比例参考。

逐条引用处理：

1. 在 `source` 中说明真实来源类型：`artifact`、`upload_token`、`asset_version` 或纯测试 `fixture`。
2. 保留 `preserve` 和 `avoid_copy`，说明要继承的特征及禁止带入的背景、文字、姿态等。
3. 根据实际核验填写 `rights` 与 `verification`，不得把 `user_asserted` 自动改成 `cleared`。
4. 由经认证宿主验证空间访问、文件可读性与引用归属；`asset_version`/`upload_token` 还依赖未来解析适配器。
5. 存在冲突时先列明冲突，选择用户指定的基线或请求澄清；不自行融合两张脸制造第三个人。

`source.kind="fixture"` 的 `verification.scope` 与 `verification.file` 都必须为 `fixture_only`。它可证明离线字段和编译逻辑，不证明参考图已存在或人物已一致。

没有参考图时，`references=[]`，四个绑定都为 `null`；没有已核验身份版本时，`canonical_version=null`。本轮未生成的提案保持 `PLANNED`，明确“文字定义的身份目标，尚无图像一致性证据”。不要制造参考 ID、批准状态或示例 Artifact 来补齐外观。

未来首次生成成功后可依据真实输出进入 DRAFT/REVIEW；这不等于文字描述已经实现稳定人物一致性。批准、绑定与下次复用仍需要各自的证据。

## 固定属性与镜头属性

`property_policy.fixed_paths` 是本版本内锁定的 AssetSpec 路径，至少一个，且每条路径必须指向当前规格中真实存在的字段。它不是允许忽略其他身份条件的白名单，也不是核心存储修改命令。

典型 Character 锁定路径：

```text
/identity
/appearance/age
/appearance/face
/appearance/hair
/appearance/skin
/appearance/body
/clothing
/color
```

Product 通常锁定 `/identity`、`/shape`、`/material`、`/color` 及相关 `/type_details`。场所/建筑应锁定布局、结构和空间关系；不要因换天气重绘建筑拓扑。

`shot_overridable_fields` 只能从 Schema 允许的 `action`、`expression`、`pose`、`composition`、`camera`、`lighting`、`setting`、`time`、`weather` 中选。它说明未来镜头可临时变化的输入，不允许向 AssetSpec 增加这些任意根字段。

临时变化放在 Skill 输入的 `scene` 中，由编译器生成这一次画面描述；不回写 AssetSpec。允许 `camera` 表示改变观察角度，不能借机修改人物脸型；允许 `lighting` 不能把产品固有颜色改成另一颜色；允许 `setting` 不能把一个固定咖啡店资产替换成另一间布局不同的店。

AssetSpec 本身的 `composition`、`camera`、`lighting` 是该资产展示图的默认方案。Scene 可按已声明策略临时覆盖展示方式，但不能越过固定身份、引用约束或用户的硬条件。

例如：

| 请求 | 处理 |
| --- | --- |
| “同一角色微笑，正面中景，傍晚暖光” | 固定版本 + 合法 scene 覆盖，不改身份规格 |
| “把角色短发改成长卷发” | 新版本提案；不能包装成 expression/pose |
| “同一瓶产品，从低角度拍” | 允许的 camera 覆盖，仍锁定形状、颜色和标识 |
| “把白瓶变成黑色方瓶” | 新产品版本提案；若已是另一商品身份则另建逻辑资产 |

## 派生与批准

修改已保存的规格或已批准版本时，从明确基底派生新的 `vN+1` 提案，不原地覆盖旧快照。编辑 DRAFT 也保留旧版本依据；不能因为尚未批准就丢弃已经形成的版本历史。未保存的同一规划草稿内的整理不应伪造已生成历史。

派生使用已有字段表达：`generation_strategy.purpose` 选择 `regenerate`、`edit_version` 或 `derive_variant`，`base_version` 记录父版本，目标 `version` 为新版本；在 `provenance.notes` 说明变更。不要为此向严格 AssetSpec 增加不存在的 `parent_version` 根字段。未来版本注册表应持久化等价 parent 关系。

版本号由未来宿主检查既有记录、分支冲突与可用编号；不能覆盖已占用的 `vN+1`。旧 `APPROVED` 版本在新提案完成前保持可用，批准指针不能随生成自动移动。

新版本升级为 APPROVED 至少需要：

1. 实际输出 Artifact 已由既有归档流程登记，文件、格式、尺寸与哈希可核对。
2. 用户明确查看并批准相应输出，不能以技术 QA 或模型自评代替。
3. 未来 host 的 AssetVersion 绑定能够验证逻辑版本、`artifact_ids`、规格快照及来源关系。
4. 用户明确选择该版本作为批准版本；需要变更身份基线时另记录对应决定。

当前工作台只有 Artifact 审核和来源血缘，没有通用 Version 注册表或唯一批准版本指针。缺第三项时可以报告“Artifact 已审核、逻辑版本绑定尚未实现”，不能宣称完整 Approved Asset 已落库。详见 [项目集成契约](project-integration.md)。

## 给后续 Shot Skill 的引用契约

镜头必须固定引用 `character_001:v3`，或等价 `{ "asset_id": "character_001", "version": "v3" }`，再附临时 scene；例如 Shot 07 使用该版本并要求“抬头、微笑、近景”。不允许用“最新版主角”“类似上次的人物”重新生成一个未绑定身份。

解析时确认版本已批准、Artifact 可访问、绑定未被篡改，并加载该版本的固定字段和引用策略。引用丢失、未批准或冲突时保留原请求，标记需要处理；不自动回退到其他版本或新人物。

本轮仅提供这些契约与离线验证，不创建 Shot/Video Skill，不真实生成角色图，也不将一致性规划写成视觉质量保证。
