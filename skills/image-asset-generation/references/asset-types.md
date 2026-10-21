# 第一版资产类型与字段规则

以 [AssetSpec Schema](../schemas/asset-spec.schema.json) 为完整合同，Character/Product 分别叠加 [character.schema.json](../schemas/character.schema.json) 与 [product.schema.json](../schemas/product.schema.json)。下面的必要字段是现有 Schema 字段，不引入隐含 API 或替代字段名。

## 先确定复用对象

一个 AssetSpec 只描述一个稳定的可复用对象或视觉方案，并且只有一个 `asset_type`。类型按“以后需要保持什么不变”选择，不能只凭所选 Style 判断。

用户要求“女主角、校园咖啡店和书包”时拆成 Character、Location、Prop 三个独立规划 packet；它们可共享 Style，但各有 `asset_id`、版本、引用和生成计划。packet 是本 Skill 的独立输入/输出单元，不是当前 AssetSpec 新增的根字段。

多资产总需求不能塞成 `asset_type="character+environment"`，也不能将整个角色设定放入 Location 的 `type_details`。本轮输出多个规划单元不意味着获准批量生成；次数和费用仍须统一呈现并确认。

## 所有类型都要保留的字段

顶层必要字段：

```text
schema_version, asset_id, asset_type, asset_name, role, description
identity, appearance, clothing, material, shape, color, environment
composition, lighting, camera, style, references, constraints
aspect_ratio, resolution, generation_strategy, preferred_model, fallback_model
status, version, type_details, consistency, property_policy, assumptions, provenance
```

其中 `identity` 必须有 `identity_description` 与非空 `visual_signature`；`appearance` 必须有 `summary`；`color` 必须有非空 `palette` 与 `locked`。这些公共字段不能因为某类型“不是人物”就删除。

展示方案也必须完整：`composition` 有 `description/framing/negative_space`；`lighting` 有 `description/mood`；`camera` 有 `viewpoint/distance/lens_intent`；`style` 有 `style_id/variables`。它们是资产图的默认展示意图，不是固定身份的替代品。

不适用与未确认分别处理：

| 字段 | 不适用或尚未指定时 |
| --- | --- |
| `clothing` | 非 Character/Wardrobe 可为 `null`；这些两类必须为含 `base/accessories` 的对象 |
| `material` | 允许的类型可用 `[]`；Product/Prop/Vehicle/Wardrobe/Food/Architecture 至少一项 |
| `shape` | 允许的类型可为 `null`；Product/Prop/Vehicle/Wardrobe/Food/Architecture 必须为非空字符串 |
| `environment` | 不适用时 `null`；Location/Environment/Architecture 必须为完整对象 |
| `references` | 无引用时 `[]`，不伪造参考图 |
| `constraints.exact_text` | 无需文字时 `null`；有指定文字则精确保存 |
| `preferred_model/fallback_model` | 未指定时 `null`，不擅自绑定模型 |
| `resolution` | 未指定为 `{ "mode": "unspecified", "width": null, "height": null }` |
| 四个 `reference_bindings` | 无对应引用时各为 `null`；不要省略键 |
| `assumptions`、可空约束数组 | 无假设或额外条件时 `[]`；不要添加虚假事实凑数 |

必填非空字符串不能用 `null`，必填非空数组不能用 `[]`。例如 Product 的 `type_details.logo` 在无标识时应明确写“无标识”；Wardrobe 的 `fastenings` 在无扣件时可明确写“无扣件，套头结构”，不能捏造扣件。

`environment` 为对象时必须有 `place`、`architecture`、`time`、`weather`、非空 `details`。自然地貌可写“无人工建筑”，室内无天气表现可写“室内，不表现室外天气”；不要伪造地点条件来填字段。

## 12 种类型

下表 `type_details.*` 均为该类型必填字段；“额外要求”是在上面通用要求之外的 Schema 约束。固定/镜头属性是 Agent 选择 `property_policy` 的依据，仍应以用户真实需求为准。

| 类型 | `type_details` 必填字段与额外要求 | 主固定属性 | 可变镜头属性示例 |
| --- | --- | --- | --- |
| `character` | `continuity_notes`；`identity.character_id`；`appearance.age/face/hair/skin/body/accessories`；非空 `clothing` 对象 | 人物身份、年龄设定、脸、头发、肤色、体型、基础服装、标志性配饰 | 动作、表情、姿势、观察角度、景别、照明、临时场景 |
| `product` | `branding/surface/logo/dimensions`；`identity.product_identity`；非空 `material` 与 `shape` | 商品身份、形状、尺寸比例、材质、固有颜色、表面工艺、标识内容与位置 | 摆放姿态、取景、背景、打光；不能因此改商品规格 |
| `location` | `space_type/layout_anchors/spatial_relationships`；完整 `environment` 对象 | 可定位场所、布局锚点、门窗与家具关系、出入口和动线 | 相机位置、取景、时间与天气表现、照明、临时活动 |
| `environment` | `environment_type/geographic_features/atmosphere/scale_cues`；完整 `environment` 对象 | 地形结构、地理特征、尺度、定义该环境的气候/氛围条件 | 视角、景别、允许变化的时段和天气、临时运动元素 |
| `prop` | `function/scale/distinguishing_marks`；非空 `material` 与 `shape` | 用途、比例、轮廓、材质、颜色、可识别磨损或记号 | 持握/放置姿态、动作状态、观察角度、背景与照明 |
| `vehicle` | `vehicle_class/propulsion/wheel_or_support_layout/scale`；非空 `material` 与 `shape` | 车型、推进方式、轮/支撑布局、车身结构、比例与涂装 | 行驶/停放姿态、方向、镜头距离、路面场景和照明 |
| `wardrobe` | `garment_type/silhouette/construction/fit/fastenings`；非空 `material/shape` 与 `clothing` 对象 | 品类、剪裁、廓形、结构、材质、颜色、扣件和基础配件 | 穿着姿态、衣料自然褶皱、展示角度与照明；不改裁片结构 |
| `style_reference` | `visual_rules/excluded_subject_attributes/target_usage` | 视觉规则、色彩与线条/纹理原则、明确不迁移的主体特征 | 示例主体与构图可按目标使用调整，但不反向改变风格规则 |
| `key_visual` | `message/hero_elements/hierarchy/safe_area` | 核心信息、主体元素、信息层级、安全区与指定文字 | 获准的展示取景、照明、背景情境；版式约束不是随意变化项 |
| `logo` | `brand_name/mark_type/geometry_rules/wordmark/minimum_size_intent` | 品牌名、图形几何、字标内容、比例、指定颜色 | 展示角度或背景只影响 mockup；不得改变标志本体 |
| `food` | `dish_name/ingredients/preparation/serving/edibility_cues`；非空 `material` 与 `shape` | 菜品身份、原料、烹饪方式、形态、份量/摆盘、可食用表现 | 相机角度、打光、背景、允许的蒸汽或切分动作 |
| `architecture` | `building_type/structural_system/facade/scale/site_relationship`；非空 `material/shape` 与完整 `environment` | 建筑类型、结构体系、立面、层数/尺度、材质、与场地关系 | 观察位置、焦距意图、时段、天气、照明和临时人物 |

表中的“可变”只表示候选项。必须在 `shot_overridable_fields` 中允许，且不冲突于固定身份与约束；未允许的属性不能自行变更。某环境以暴风雪为定义条件时，“晴朗天气”不是普通镜头变化；应形成新版本或不同环境资产。

## 易混淆类型的选择

**Product / Prop**：商品外观、品牌与商业规格需要反复一致时选 Product；叙事中使用的物件、功能与辨识记号更关键时选 Prop。科技设备可属于任一类，按复用目的决定，不能因“科技风格”另造类型。

**Location / Environment / Architecture**：需要复用“同一间咖啡店的空间关系”选 Location；需要复用“同一片高原的地形与氛围”选 Environment；需要复用“这栋楼的结构和立面设计”选 Architecture。一个故事可能同时需要三者，分别规划，不把三套字段混进一个 `type_details`。

**Character / Wardrobe**：角色基础服装是 Character 的固定属性；需要独立复用或替换的一套服装可单独建 Wardrobe。独立服装资产不授权自动覆盖已有 Character 的基础服装，换装需要明确的版本或派生方案。

**Style / Style Reference**：`style.style_id` 选结构化风格规则；`asset_type="style_reference"` 是准备生成并复用的视觉参考资产。风格系统不是一张参考图，参考图中的人物、品牌、文字也不应随着 Style 一起复制。

**Key Visual / Logo**：广告主视觉或海报布局选 Key Visual；标志本体选 Logo。海报含已授权 Logo 时，应精确引用标志资产并限制修改；不能为了生成海报重新设计品牌。当前图片后端输出位图，Logo 规划不代表已交付矢量路径或商标注册结论。

## 分类后执行顺序

1. 识别一个或多个独立复用对象，分别确定类型、逻辑 ID、角色和描述。
2. 填完整通用字段与该类型的 `type_details`；不清楚的重要身份条件先澄清，低影响假设写入 `assumptions`。
3. 把同版本必须不变的字段列入 `fixed_paths`，把允许的临时画面变化留给输入 `scene`。
4. 处理引用用途、授权和验证状态；没有参考时保留空数组/null，不制造真实性证据。
5. 根据视觉意图选 Style，将主体固有颜色与 Style 调色区分，防止风格覆盖固定身份。
6. 编译分层 Prompt，检查任务类型、比例、尺寸、参考数量与当前 Backend 能力。
7. 输出有限生成计划与待报价状态；本轮保持 PLANNED，不进入真实生成。

需要修改脸、发型、体型、基础服装、产品颜色/形状或其他已锁定属性时，按照 [一致性规则](consistency.md) 创建新版本提案。镜头变化不得回写同版本 AssetSpec。

## Agent 检查清单

- `asset_type` 是 12 个枚举之一，且每个 packet 只有一个类型。
- `identity.visual_signature` 具有可区分的可见特征；`role` 描述用途，不充当类型替代。
- Character 的 `identity.character_id == asset_id`，必要外观字段和衣服对象齐全。
- Product 的尺寸、材质、形状、表面与标识有明确约束；不虚构品牌授权。
- 类型专属数组满足 `minItems`；不适用的通用值依 Schema 保留 `null/[]`，不删除必要键。
- `references`、四种绑定、`canonical_version` 与真实来源/验证状态一致。
- 类型细节与 `constraints`、`fixed_paths` 不冲突；临时 scene 不改对象身份。
- `schema_version`、逻辑 `version`、来源 `base_version` 各表达自己的含义。
- 必须真实执行才能成立的 Artifact/Version/APPROVED 状态没有被规划样例冒充。

这些检查验证结构与语义完整性，不验证最终图像的审美、身份相似度或真实物理准确性；后者需要生成后的人工核查，且不在本轮真实调用范围内。
