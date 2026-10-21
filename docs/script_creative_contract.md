# Script Template & Style Engine — 本地增量接口

本轮只开发和测试，不推送或部署。沿用 ScriptService、Model Registry、Codex Adapter、StorageManager、Artifact/Version 和积分账本。

## 数据

- 系统模板继续登记在 `skills/video-script-generation/registry/templates.json`，保留旧字段和 ID；新增 `version`、`structure`（`section/purpose` 对象数组）、`required_fields`、`optional_fields`、`output_format`、`duration_rules`、`examples`、`keywords`。10 个新模板，旧 2 个仍可用。
- 风格登记在同目录 `styles.json`，`schema_version:1, styles:[]`；每项含 `style_id,name,description,version,enabled,narrative_pacing,dialogue_style,emotional_tone,conflict_style,visual_language,characterization,constraints,prompt_guidance,examples,keywords`。constraints/prompt_guidance/examples/keywords 为数组，其余叙事特征为字符串。
- 私有 Example 和 Template 按 account + workspace 隔离，所有资源修改新建整数版本，停用仅更新最新版本，不删除历史。新 ID 为 `example_<uuidhex>` / `personal_<uuidhex>`。
- 个人模板含 `template_id,workspace_id,owner_account_id,name,description,type,source_type,source_artifact_id,source_version,source_example_id,extraction_method,analysis_model,structure,required_fields,optional_fields,variables,style_guidance,format_rules,prompt_guidance,created_at,updated_at,version,visibility,enabled`。variables 是 `{name,label,default}` 数组；visibility 固定 private。来源字段由服务器草稿决定，不能由客户端越权替换。
- Example 含 `example_id,version,workspace_id,owner_account_id,main_project_id,subproject_id,name,generation_type,reference_roles,enabled,sha256,filename,text,analysis`；TXT/DOCX/PDF 最大 5 MiB，解析文本最大 200000 字符，原件保留；无 OCR。

## HTTP（所有写接口沿用 CSRF 和当前 Workspace）

`GET /api/scripts/config` 增加 `styles`，templates 合并 system/personal（以 `origin` 区分）。

`POST /api/scripts/quote` 和 `/tasks` 增加：
`template_version?:int, style_ids:string[], example_refs:[{example_id,version?:int,reference_roles:string[]}], variables:object, conflicts_confirmed:bool`。
`template_id:"none"` 显式自由生成；省略保留原来默认模板行为。引用角色：structure/style/dialogue/format/comprehensive。
Quote 返回 `conflicts`；存在冲突而未确认时 can_generate=false，submit 拒绝。用户约束始终保留。

- `GET /api/scripts/examples?subproject_id=...` → `{examples:[...]}`（只列本人的当前工作区，默认不带全文）
- `POST /api/scripts/examples` multipart：`file, subproject_id, generation_type, reference_roles`（roles 为 JSON 数组字符串）→ example（含 text 预览）
- `GET /api/scripts/examples/{id}` → example
- `POST /api/scripts/examples/{id}/analyze` JSON `{}` → `{analysis,provider,requires_confirmation:true}`，只生成待审核结果
- `PATCH /api/scripts/examples/{id}` JSON `{expected_version,analysis?,reference_roles?,enabled?,name?}` → 新版 example；分析保存即用户确认
- `GET /api/scripts/examples/{id}/download?version=...` → 原件
- `POST /api/scripts/recommend` JSON `{input_request,generation_type}` → `{recommended_template,recommended_styles,alternatives,match_score,score_method,reasons,missing_information,provider}`；推荐模板和风格项为对应资源对象，不自动应用
- `GET /api/scripts/templates?include_disabled=true` → `{templates:[个人模板]}`
- `GET /api/scripts/templates/{id}` → 个人模板含 `versions`（各版摘要）
- `POST /api/scripts/template-drafts` JSON `{source_type:"existing_artifact"|"uploaded_file"|"manual",task_id?,example_id?,extraction_method:"structure"|"ai",type,name?,description?}` → `{draft_id,template,source_preview,provider,notice}`；默认 AI 为明确标记的规则预览，不进行收费调用
- `POST /api/scripts/templates` JSON `{draft_id,confirmed:true,template:{可编辑字段}}` → 新个人模板；无确认不保存，重复确认同草稿返回同资源
- `PATCH /api/scripts/templates/{id}` JSON `{expected_version,template:{可编辑字段}}` → 新版本；过期版本返回 409
- `POST /api/scripts/templates/{id}/copy` JSON `{name?}` → 独立副本
- `GET /api/scripts/templates/{id}/versions/{version}` → 历史版本

可编辑字段：name,description,type,structure,required_fields,optional_fields,variables,style_guidance,format_rules,prompt_guidance,enabled。模板结构及规则必须是提炼结果，提取器不将作品正文保存为模板。

## 冻结与验收

报价指纹包含实际引用的完整内容摘要。任务保存不可变 creative_snapshot（模板、风格、案例版本/内容摘要和有限片段）；Version 保留来源追踪。后续资源编辑或停用不会影响已排队任务。基础结构检查报告缺项/截断疑点供用户查看，不触发模型重试；UTF-8、空正文等技术错误沿用既有失败退款机制。

DeepSeek routing 配置需核实现状，未配置则免费规则分析 + 可注入 Mock 接口；UI 明确显示分析方式，不冒充真实 AI 分析。真实付费调用默认禁用。
