# 宿主接口

执行实现使用项目现有 Python 后端、账号与项目边界、积分账本、StorageManager 和 Codex 调用能力。Skill 的模板与案例数据在此目录维护，不引入第二套账户、积分或文件系统。

## Template / Example Loader

模块：`image_workbench.script_templates`

- `ScriptTemplateLoader(root: Path | None = None)`：默认使用当前 Skill 目录；可在隔离测试中指定目录。
- `list_templates(generation_type=None)`：返回启用的模板元数据，可按 `script` 或 `screenplay` 过滤。
- `load(template_id, generation_type)`：返回登记字段及 `template_text`、`template_reference={path,sha256}`、`example_references=[{登记元数据,path,text}]`。路径相对于 Skill 根目录。
- `build_prompt(request, template, parent_text=None)`：根据已提供字段与已加载内容编译生成任务。`input_request` 归一化为 Prompt 的 `user_request`；支持 `duration`、`aspect_ratio`、`target_audience`、`tone`、`characters`、`product_ip`、`constraints`、`change_reason`。模型和积分元数据不作为作品内容字段。
- `validate_generated_text(text)`：拒绝空白、超过 256 KiB 的 UTF-8 正文、整段 JSON、代码围栏、HTML 封装、明确内部调试或思考标签。返回统一 LF 换行、末尾单换行的文本；无效内容抛出 `ValueError`。

Loader 仅读取已登记 ID、受限相对路径、对应类型目录内的 `.txt`，检查 UTF-8、大小和案例 SHA-256。模板或案例数据更新后，调用时重新加载；Task 保存当次 template_reference 与 example_references 的 hash，不用历史任务自动继承新文件内容。

## 独立创作资源

`script_resources.ScriptResourceStore` 管理 account + workspace 私有案例、模板草稿和不可覆盖的版本；`script_engine.resolve_resources()` 统一解析系统模板、个人模板、独立风格和多个案例。`template_id="none"` 表示自由生成。原有缺省模板选择保持兼容。

`list_styles()` 读取独立风格配置。每次提交保存 `resource_references` 与私有 `creative_snapshot`，版本记录保留同一份来源引用；执行读取提交时已编译的 Prompt。个人模板更新、案例分析确认和停用均产生新版本。`script_resource_web` 承载上传、规则分析、推荐、模板草稿和管理接口，沿用当前鉴权和 CSRF。

`RuleExampleAnalyzer` 为免费规则实现；`MockExampleAnalyzer` 是可注入测试替身。真实轻量模型尚未接入本轮资源分析，不新增收费项目。分析是待审核建议；确认后才纳入案例引用。结构检查为提示，不触发自动模型重试。

完整 Schema、HTTP 和大小限制以 [创作资源契约](../../../docs/script_creative_contract.md) 为准，实施与验证见 [交付报告](../../../docs/script_template_style_engine.md)。

## 模型、价格与任务

配置入口分别为仓库 `config/script_models.json`、`config/script_pricing.json`、`config/script_routing.json`。模型支持档位和可用性由宿主实际发现；价格独立配置，未定价组合不可提交。手动模型选择不得被推荐覆盖。

宿主将模板、案例引用、解析后的模型/思考档位、固定报价与用户输入关联到 Task。积分检查及冻结先于调用；登记 TXT Artifact 和版本成功后结算；失败释放冻结金额。父版本只读加载，修订创建新版本。

## 后续 Storyboard 接口

下游读取选定的 `output_artifact_id`、`output_version` 和 TXT 正文，以及当前项目/子项目、`generation_type`、实际提供的 duration/aspect_ratio/characters/constraints。不可变版本是依据，不能偷偷换用最新草稿。下游自行解析场景与人物，单独进行分镜规划、报价和执行。本 Skill 不预先生成 Shot 或图片资产，也不把最终 TXT 替换为结构化 JSON。
