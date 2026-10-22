# 第四阶段本地 Web 接口约定

图片兼容扩展（2026-10-22）：新增经原身份/Workspace 入口访问的 `GET /api/image-styles`，返回
`{skill_id,version,default_enabled:true,styles:[{id,name,category,summary,tags,prompt,example_prompt,aspect_ratio,preview_url,preview_alt,sources}]}`。
任务创建 multipart 增加可选 `style_id`；不传或空值均表示遵循原始需求，但仍使用默认图片 Skill。
未知风格返回 400，同一保存请求改变风格返回 409。旧请求字段、路径及提交/计费边界不变。
TaskDetail 增加 `image_prompt`（旧记录为 null），包含 `skill_id,skill_version,skill_sha256,style_id,style_name,source_prompt,compiled_prompt,spec`；
任务列表不携带该较大的快照。预览是随网站发布的公共示例，选择风格不会上传或绑定预览图。
完整调用与兼容策略见 [图片默认 Skill](image_generation_skill.md)。

后续兼容扩展（2026-10-20）：脚本页面和 `/api/scripts/*` 路径、请求字段与已有响应字段保持兼容。
`GET /api/scripts/config` 增加 `generation_plugin`（描述及 `enabled`），不替代模型目录的实际可用性。
脚本插件停用后，新 quote/submit 返回 409；相同已受理请求的 submit 仍返回原任务，
detail/list/download/recover 和手动版本入口继续按原权限与恢复合同工作。
这是注册表明确停用时的新增行为，默认启用保持原流程。没有增加公开启停写接口。

仅 localhost；同源请求。所有写操作附 `X-Workbench-CSRF`，值来自 `GET /api/config`。所有ID为32位小写十六进制；网页不传服务器路径。JSON错误为 `{detail: "可读中文摘要"}`，不返回内部异常或日志。

- `GET /api/config` → `{csrf_token, backend: "codex"|"local", worker_running, max_input_images:8, max_image_bytes:20971520, max_upload_bytes:41943040, ratios:["auto","1:1","3:2","2:3"]}`。local显示“演练模式 · 不消耗生图额度”；codex显示“本地 Codex · 手动启动”。比例由提示词约束，不保证精确像素尺寸。
- `GET /api/projects` → `{projects:[{project_id,name,created_at}]}`。
- `POST /api/projects`，JSON `{request_id,name}` → Project。request_id在同一次保存意图内固定，重发不创建重复项目。
- `GET /api/projects/{pid}/tasks` → `{tasks:[{task_id,project_id,operation,prompt,base_artifact_id,created_at,status,status_label,input_count,output_count,preview_artifact:ArtifactDTO|null}]}`，新到旧。摘要包含首张输出预览，不必逐项打开详情才能看到缩略图。
- `POST /api/projects/{pid}/tasks`，multipart：`request_id`, `operation` generate/edit, `prompt`, `aspect_ratio` auto/1:1/3:2/2:3, `base_artifact_id`可选，`files`可重复上传。→ 完整TaskDetail。仅保存任务，不启动模型；同一request_id重发返回同一任务，不同内容冲突409。
- `GET /api/projects/{pid}/tasks/{tid}` → TaskDetail（见下）。
- `POST /api/projects/{pid}/tasks/{tid}/run`，JSON `{request_id,confirm_generation:true,retry_of?:job_id或last_execution_id}` → `{job_id,status}`。普通重复提交返回既有作业；失败后只有显式retry_of才能重新执行；成功任务不能再次执行，可新建任务。后端无需浏览器等待。
- `POST /api/projects/{pid}/tasks/{tid}/restore`，JSON `{request_id,execution_id}` → `{job_id,status}`；仅恢复已验证候选归档，不调用模型。
- `POST /api/artifacts/{aid}/approve`，JSON `{}` → ArtifactDTO；只用于输出图片，重发幂等。
- `GET /api/artifacts/{aid}` → 已登记图片原文件，图片预览地址；`?download=1` 以附件下载。

TaskDetail在任务摘要基础上增加：`inputs:[ArtifactDTO]`, `outputs:[ArtifactDTO]`, `base_image:ArtifactDTO|null`, `aspect_ratio`, `current_job:{job_id,status,created_at,started_at,finished_at}|null`, `last_execution_id`, `can_generate`, `can_retry`, `can_restore`, `error_summary`, `failure_type`, `elapsed_seconds`, `timings:{prepare_seconds,codex_seconds,validate_seconds,register_seconds,total_seconds}|null`, `executions:[{execution_id,status,created_at,failure_type,total_seconds}]`。API不返回原始stderr、命令、凭据或磁盘路径。

ArtifactDTO：`artifact_id,project_id,task_id,kind,role,format,width,height,size_bytes,created_at,source_artifact_id,url,download_url,review_status:"pending"|"approved",reviewed_at`。

界面status：`draft`待开始、`queued`等待执行、`generating`生成中、`checking`检查产物、`awaiting_review`等待审核、`approved`已通过、`failed`执行失败、`uncertain`状态待确认。

前端使用textContent显示项目名、提示词、错误和ID；不把用户内容注入innerHTML。静态HTML片段可以使用模板。JS/CSS使用独立文件，不引入CDN或内联脚本。禁止自动发run/restore；这些接口只在按钮点击时调用。刷新只读取状态。

已选中的 queued/generating/checking/uncertain 任务每3秒只读刷新状态；CLI批测结束后，即使先前显示状态待确认，也会自动读到归档结果。状态轮询不会重放生图。

持久化请求ID：保存任务的request_id跨网络重试不变；请求进行时禁用按钮。多个标签页即使request_id不同，后端也会按task_id去重执行。失败重试使用当前job_id（历史任务无job时使用last_execution_id）作为retry_of。

点击任意输出“基于此图继续修改”：切换到该图所属项目，建立新的编辑草稿，将artifact_id放入base_artifact_id并展示基底预览；不直接执行。所有历史版本都可作为分支基底。
# Asset UX 补充（2026-10-20）

- `GET/POST /api/subprojects/{subproject_id}/assets` 列出或上传当前子项目图片。上传使用 multipart `files`；后台校验图片内容、每张配置上限、每批 1–9 张与 40 MiB 总量。同子项目相同内容返回已有 Artifact ID。权限仍由当前 Account/Workspace 校验。
- `POST /api/video/projects/from-subproject` 用 `subproject_id` 获取或创建指向同一 MainProject/SubProject 的视频项目。
- `GET/POST /api/video/projects/{project_id}/tray` 读取或调整暂存引用。POST 字段为 `artifact_id`、`media_type`、`action`（`add`/`remove`）及可选 `shot_id`；移出不删除原素材。
- `POST /api/video/projects/{project_id}/shots/{shot_id}/clip` 以 `source_shot_id`、`artifact_id` 绑定同项目已完成视频；不能覆盖正在执行或已有成片的镜头。
- `POST /api/scripts/tasks` 的 `quote_signature` 可省略。服务端仍从当前 Registry 计算真实价格并执行额度、模型可用性和幂等校验；若传入签名，继续校验签名有效性。旧客户端行为兼容，客户端提交的未知价格字段仍被拒绝。

