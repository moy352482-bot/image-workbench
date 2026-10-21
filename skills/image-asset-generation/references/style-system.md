# Style System 与 Selector

权威结构为 [style.schema.json](../schemas/style.schema.json)，索引为 [styles/index.json](../styles/index.json)。每份 Style 有稳定 slug、schema_version、视觉定位、类型白名单、6 个视觉维度、核心/适配锚点、变量、模板、负向约束、参考排除项、至少两个原创例子及模型无关说明。schema_version 表示格式版本；Style 内容更新另以内容 SHA-256 固定，不能仅凭同一 slug 复现旧版本。

## 选择顺序

1. 从用户目的确认主资产类型：人、商品、空间还是图形。一个人站在咖啡店的请求若需要分别复用，则拆人物和 location，不能把整个画面当作单个人物身份。
2. 提取视觉意图：可信记录、情绪叙事、商品辨识、服装展示、图形传播、建筑结构等；不要用“高级”“好看”直接代替可解释的选择依据。
3. 按 type 过滤候选，再用 visual_intents、natural_language_cues、avoid_when 和核心锚点匹配。用户指定风格优先，明显冲突时解释并澄清或给有理由的替代计划。
4. 按目标选主风格，避免平均混合多个互相矛盾的风格。主风格之外的单项灯光/配色意图写入 AssetSpec 或变量，不能临时声称新风格已入库。
5. 显式填写所有变量，记录采用默认值的假设；用明确理由而非伪造精确评分解释选择。两个候选同样合适但不影响目标时选一个可逆默认；重大分歧才问用户。

| 自然语言意图 | 推荐类别与候选 | 选择依据 |
|---|---|---|
| 高端护肤品广告 | photography / commercial-product | 产品形状、表面、标识优先，光线表现材质 |
| 成年大学女生的青春故事 | photography / cinematic-realism；环境另建 location | 合理窗光与空间层次，固定人物不随剧情变脸 |
| 不摆拍的咖啡店日常 | photography / lifestyle 或 documentary | 前者强调自然生活气氛，后者强调记录性与现场光 |
| 动漫角色设定 | illustration / anime-manga | 线条、色块与可复用轮廓，不绑定某部作品角色 |
| 产品功能发布主视觉 | rendering / futuristic-sci-fi，或 graphic / graphic-poster | 取决于需要未来空间还是文字信息层级 |
| 建筑结构及入口展示 | architecture / architectural | 空间关系、尺度、透视优先 |

## 固定的 15 种基础风格

| slug | 视觉作用 |
|---|---|
| cinematic-realism | 可信光源、情绪与空间分层 |
| commercial-product | 产品结构和材质的商业展示 |
| editorial-fashion | 服装剪裁、姿态与编辑摄影 |
| documentary | 现场光和非表演性的记录 |
| anime-manga | 清晰线条、色块和角色轮廓 |
| illustration | 有组织的二维造型和叙事表达 |
| watercolor | 透明叠色、纸张和边缘变化 |
| 3d-stylized | 简化体块与一致的三维材质 |
| retro-film | 克制的胶片颗粒、色彩与光学感 |
| futuristic-sci-fi | 功能可信的未来结构与光环境 |
| minimal | 留白、有限颜色和明确视觉焦点 |
| graphic-poster | 信息层级、图形节奏和文字区域 |
| lifestyle | 日常空间、自然姿态和生活气氛 |
| architectural | 结构、尺度、垂直线和场地关系 |
| food-photography | 食物质感、烹饪状态和可食用细节 |

## 变量与冲突

模板仅使用 `{{lower_snake_name}}`。自定义变量不可覆盖保留的 `asset_name / asset_type / scene_summary`；实际模板只需使用相关的保留变量。所有自定义变量须被模板使用，所有出现的占位符必须有定义。枚举默认值/案例值必须属于 choices；未知变量、空值或残留占位符报错。插值不解析 shell、Python、Jinja 或其他表达式。

变量只能调节风格层；不能覆盖 identity、固定材质、固定颜色、精确文字或输出规格。例如复古色调不能把红色产品变成棕色，极简风格不能删除用户要求的 logo，动漫风不能把既有角色的脸型特征换掉。明确用户目标和固定资产约束优先；若核心锚点因此无法成立，应换风格并记录理由或澄清，不能悄悄丢弃核心锚点。

Style 的 negative_constraints 是候选限制，须检查条件与正向要求，再加入最终负向层。禁止把“无文字、无颗粒、无 logo”当成全局口令。source_content_to_avoid 用于约束参考迁移，不能自动删除用户自己的身份/品牌。

## 模型边界

Style 描述视觉表达，不含 provider URL、模型版本、专有质量值、采样器或价格。换模型时保留 Style 与 AssetSpec；由未来 Model Adapter 把视觉意图映射到真实支持的字段，无法映射的参数显式报告。当前没有参数支持，不等于可以用 Prompt 保证它实现。
