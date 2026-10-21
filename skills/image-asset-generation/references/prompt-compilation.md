# Prompt Compiler 设计契约

本版交付编译规则与可验证的离线样例，不提供可执行生产编译器。Agent 负责语义规划，测试中的插值/拼接只是验证既定数据闭合，不是自然语言理解模型。

## 输入、输出和阶段

输入为 AssetSpec、已选 Style 内容快照、Reference 及角色、generation_intent、scene、目标比例/分辨率/质量。输出是最终 Prompt、12 层文本、generation parameters、reference strategy、去冲突后的 negative constraints。当前 packet 对接 [输出 Schema](../schemas/skill-output.schema.json)。

先校验字段和类型专属要求，再解析固定路径/场景覆盖，再选择风格变量与参考职责，最后形成 12 层。源对象保持不变，不将某次镜头灯光或动作写回固定身份。文本输出用统一层标识和换行串联；视觉内容与调用参数分别保留，保证可以检查未透传的要求。

| 顺序 / 标识 | 内容来源与要求 |
|---|---|
| 1 asset_identity | asset_id、版本、类型、身份描述、visual_signature；人物含 character_id |
| 2 subject_description | appearance、clothing、shape、color 及类型详情；具体可识别，不只写“好看” |
| 3 scene | environment 与允许的 scene.setting/time/weather；固定空间结构不被覆盖 |
| 4 action_state | scene.action/expression/pose；无动作请求时写清静态状态，不能自动加剧情 |
| 5 composition | composition 与允许的镜头构图覆盖；重要部件、负空间和文字安全区 |
| 6 camera | camera 或允许覆盖；高度、方向、距离与透视意图 |
| 7 lighting | lighting 或允许覆盖；主光来源、方向、对比和保留细节 |
| 8 material_texture | material、surface、fabric 等；固定材质不能随风格改变 |
| 9 style | 模板插值、6 维视觉语言、核心锚点；条件冲突先消解 |
| 10 reference_constraints | 每个 reference_id 的角色、保留与不迁移内容；底图和风格图分开 |
| 11 consistency_constraints | 固定路径值、canonical version、角色四参考绑定；重复明确高风险身份项 |
| 12 negative_constraints | 资产 must_not + 适用风格负项 + 参考 avoid_copy；去重并消解正负矛盾 |

每层都存在。没有输入参考时写“无输入参考，不声称继承已有视觉身份”，而不是填不存在的附件；不适用材质时明确是平面颜色或未指定。最终文本不能遗留模板占位符。

## 约束优先与编译失败

明确的用户目标/已确认固定资产身份 → 对应角色的已授权参考 → 允许的 scene 变化 → Style 核心锚点 → 风格默认/建议。参考图不是比用户目标更高的指令，风格图也不是身份图。

- 若用户修订身份，生成新版本提案；不能一边声称锁定旧脸一边修改它。
- 参考之间对脸/形状有分歧时，以明确指定的 canonical 身份参考为准；同等级矛盾需澄清，不按图片顺序随机决定。
- 精确文案使用 `constraints.exact_text` 原文，不翻译或润色。负向词与所需文案/logo/颗粒冲突时删除或条件化相应负项并记录。
- 未知变量、引用不存在、fixed path 不存在、镜头越权修改、必需字段缺失属于编译错误。完整 Schema 中以“待解析已有版本”明确标注的未知身份字段仅供提案记录，必须列 `ASSET_IDENTITY_UNRESOLVED` 及字段路径；不能把非空占位文字解释为已经确定的身份。能力缺口、未报价和未绑定权限属于计划 blocker；不能假装编译文本能修复。

## Generation Parameters 和 Reference Strategy

参数保留 requested 与 applied。当前源码能力快照只允许 applied.aspect_ratio 为 auto/1:1/3:2/2:3；这些仍是 Prompt 指导而非像素保证。16:9 等其他比例保留 requested，applied 为 null，并阻止提交。若用户同意改规格，应生成新计划，不能复用旧确认。

quality 只影响规划中的细节要求，applied.quality=null；分辨率 target 可写入意图但不保证，exact 没有支持则 blocker，applied.resolution=null。不将负向列表发送到不存在的 negative_prompt 字段；暂时作为自然语言限制整合入 Prompt。所有参数判断都有 supported/prompt_only/unsupported/requires_adapter/unverified 与原因。

generate 没有底图，输入均为额外参考。edit 明确一个底图，必须由宿主核实为同项目已登记 output，其余最多 8 张参考；底图不能在额外参考里重复计数。inpaint 缺原生 mask 支持，既不悄悄改成 edit，也不假称可精确局部替换。multi_reference 根据是否存在编辑底图映射操作，不建立第四个伪 API。

每步 `reference_ids` 是外部输入参考集合，非空的 `edit_base_reference` 必须属于该集合；缺底图时留 null 并列 EDIT_BASE_REQUIRED，不构造假的 reference_id。`references_from_steps` 表示上步 output 作为额外参考，并必须同时在 depends_on 内。对应的 `step_reference_constraints` 逐一明确 source_step、roles、preserve 和 avoid_copy。每步 `step_prompt` 都是完整文本，包含共有约束及本步目标；第二步不能无意重做第一步。

v1 的两步方案是先 generate 基础图、再 generate 参考图；若要以上步 output 当 edit 底图，先等实际 Artifact 登记，再形成绑定该 Artifact 的新计划，不能虚构未来底图 ID。

成本中 reference_count = 外部参考去重数 + 上步参考数（edit 底图已包含一次），image_count=1。steps 数就是计划调用次数；不自动并发、重试或产出多候选。depends_on 必须指向更早步骤，排除循环、重名和未来引用。多资产需求的总预算由宿主统一控制。

## 提交边界

规划对象始终 `execution_enabled=false`。未来宿主应把 scope、AssetSpec/Style/Prompt/参考内容哈希、请求参数、模型、价格、预算、授权范围和幂等键绑定；这不是当前已有的 plan_hash API。重新报价、改变模型或多一次调用均需检查原授权是否仍覆盖。未知结果先恢复原任务状态，不重新编译后盲重发。

所有 fixture 参考、未验证引用、未知许可、缺底图、缺 mask、未支持能力、未报价、未绑定宿主以及超过预算都应显式列出。离线结果不含真实 task/artifact ID，不能被误当成提交回执。
