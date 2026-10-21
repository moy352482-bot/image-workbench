# Skill 离线自测规范

运行 [validate_skill.py](../tests/validate_skill.py) 验证 Schema、Style、已编写的请求/期望输出和对抗性错误变体。测试不调用项目服务，不导入 `image_workbench`，不读取真实业务数据或图片，不执行模型、积分操作或外部 API。

## 运行

使用独立 Python 环境并安装 [tests/requirements.txt](../tests/requirements.txt) 的两个固定版本依赖；安装是准备步骤，验证器本身不联网、不自动安装依赖。

从项目根目录运行：

```powershell
python skills/image-asset-generation/tests/validate_skill.py
python skills/image-asset-generation/tests/validate_skill.py --output skills/image-asset-generation/tests/reports/validation.json
```

若依赖已通过 `pip --target` 安装在独立目录，可在当前进程设置 `PYTHONPATH` 后用项目 `.venv/Scripts/python.exe` 运行；不要为了本 Skill 修改应用运行依赖或系统配置。报告路径为唯一可写参数；未给 `--output` 时只打印 JSON。Skill 内部只允许写 `tests/reports/**` 下的 `.json` 报告，外部 `.json` 路径可显式指定；不能用报告覆盖 examples、Schema、Style 或其他 Skill 资源。验证器含 2 个允许路径与 5 个拒绝路径的纯路径检查，不写这些测试路径。

退出码 0 仅表示全部离线合同检查通过；任何 Schema、语义规则、正例或负例失败都返回非零。缺少示例文件也失败，不跳过所需测试。输出报告列出计数、验证范围与限制，不声称“自然语言需求已自动理解”或“图片效果通过”。

## 本地 Schema 与资源检查

- 对当前 11 份 Draft 2020-12 Schema 逐份执行元 Schema 检查，要求唯一 `$id`。
- 所有 `$ref` 显式注册在本地 `referencing.Registry`；未登记资源的检索函数直接拒绝，绝不请求网络。
- 检查引用闭合和片段存在；另用未登记 URL 验证失败关闭行为。
- JSON 重复键直接报错，避免普通解析器以最后一个值静默覆盖。
- 对案例分别验证输入、完整 AssetSpec、Character/Product 专属 Schema、生成计划和完整输出。

## Style 与 30 个使用示例

索引必须精确覆盖第一版指定的 15 个风格，索引 slug、目录与 `style_id` 对齐，且目录不能越界。每份 Style 通过自身 Schema，并满足：

1. 自定义变量不得覆盖保留变量 `asset_name / asset_type / scene_summary`。
2. 模板占位符必须有来源；自定义变量必须被模板使用，不接受表达式或未闭合占位符。
3. 默认值与示例变量必须完整，枚举值必须属于 choices；不允许嵌套模板。
4. 锚点 ID、示例 ID 唯一，至少存在核心锚点；示例类型属于风格适用类型。
5. 至少 2 个示例可用纯文本替换完成插值，不遗留变量。

这只验证模板数据闭合与应用规则，不评价视觉定位是否美观，也不将 Style 示例的主体迁移到用户资产。

## 完整请求到输出案例

[asset-generation-examples.json](../examples/asset-generation-examples.json) 使用 `cases[]`；每例含 `case_id`、`label`、`input`、`expected_output`。至少 15 个完整案例，覆盖人物、产品、咖啡店、科技产品、时尚、动漫、电影场景、食物、建筑、电商、海报、极简、复古、3D 和视频主角等用户需求。

验证器读取预先编写的 AssetSpec 和期望输出，不从 `user_request` 推理生成对象。每例检查：

- 输出/AssetSpec 的资产 ID、类型、状态、版本、参考和 Style 一致，输入的明确要求与默认参数没有静默替换。
- Character ID 与资产 ID 相等；固定路径真实可解析；四种参考绑定指向唯一且角色匹配的引用。
- fixture 只能使用 `fixture_only`，普通引用不能冒用此标记；没有参考不能虚构 canonical 身份基线。
- 目标版本高于基底版本；已有资产引用保留身份与父版本，scene 只能使用许可的镜头字段。
- 12 层按合同顺序存在；最终 Prompt 严格以 `[01 asset_identity]` 等标识、单换行内容、双换行分层拼接。
- Style 层包含完整变量插值后的模板；参考策略与参考角色对齐，负向项和精确文字不丢失。
- 步骤 ID 唯一，依赖仅指向较早步骤，上步输出引用必有依赖及同源的角色/保留/不迁移约束；每步具有完整 12 层 `step_prompt`，禁止未知参考、循环与未来引用。
- 每步一张图，步骤数等于计划调用数，费用项目与步骤一一对应；外部参考与上步输出计数相加，edit 底图只计一次。
- 超预算、16:9 等不支持比例、exact 分辨率、inpaint、缺 mask、未验证参考、未解决权利和超参考限制均具备所需 blocker。
- 所有规划必须包含 `HOST_ADAPTER_REQUIRED / COST_UNQUOTED / MODEL_UNRESOLVED`，保持 unbound、未报价与执行关闭。

这里的“参考边界验证”只检查合同是否要求阻止未核验引用，不能证明某个真实账号有访问权限。没有读取用户数据，不会把伪造 `verified` 字符串当作真实权限探针结果。

## 未来版本与技术检查合同

验证器在内存中构造明确标记为 synthetic 的 QualityCheck 与 AssetVersion 合同正例，覆盖未运行、通过、失败以及 PLANNED/DRAFT/REVIEW/APPROVED 的结构关系。它们不保存到生产注册表，也不对应真实图片或人工审核。

8 项技术检查（包括 `content_integrity`）必须各出现一次；overall 与各项状态一致。版本父子关系递增，Artifact/Task ID 不重复，质量记录属于相同资产及版本。REVIEW/APPROVED 必须有通过的技术合同；APPROVED 要有人工记录、真实登记形状及覆盖所有 Artifact 的哈希列表；PLANNED 不得携带产物或完成的检查。

上述合成正例只测试合同能表达未来结果；实际文件、真实人工操作、哈希内容及生产登记必须由未来宿主核实，见 [项目集成契约](project-integration.md) 和 [生命周期与技术检查](lifecycle-and-quality.md)。

## 对抗性负例

至少 20 个单独变异，全部必须拒绝。先验证各变异基底是合法正例，再修改一个错误条件；只有预期的数据/Schema/语义异常算成功拒绝，测试程序崩溃不算。

当前覆盖缺少人物 face、产品空 material、身份错位、路径不存在、引用缺失/角色错误/重复、fixture 冒充已验证、未验证空间引用缺 blocker、权利未知缺 blocker、输出重复字段不一致、缺层/乱序/最终文本不一致、未知风格变量、步骤重复/循环/丢依赖、错误 edit 基底、次数不符、重复计参考费、超预算及能力缺口未阻止、自动执行、伪造产物与批准、未递增版本等。

另检查模板未知变量/未使用变量/枚举错误/错误示例类型，以及质量检查重复/不完整/overall 矛盾/自动审美字段、批准缺人工记录/产物/登记、质量绑定错资产或版本、批准哈希数量不足和日期格式错误。

不得通过删除失败案例、降低门槛或只验证预期输出的字段存在来宣称通过。发现 Schema 与文档/样例冲突时，应明确报告差异并修正权威合同或样例，不在验证器中为个别 case ID 添加豁免。

## 结果边界

离线通过证明结构约束、字段关系、能力 blocker、模板闭合及已列负例可被验证。它不证明 Agent 能正确理解任意自然语言、参考图片真实合法可访问、模型支持所请求能力、计费准确落账、视觉一致性、审美质量或生产版本已登记。

真实模型、账户、图片、生产注册表、并发运行与真实批准流程均不在本轮测试范围内；不得为了让这些项目变成“通过”而启动生成或消耗额度。
