# 两级项目接口与数据契约

逻辑关系唯一保存在 `.project_management/catalog.json`。主项目、子项目、任务归属、显示名称和描述都以此为准；不在旧 Task JSON 中追加第二份 subproject_id。旧 `project_id` 在 StorageManager、TaskRunner、Artifact 和作业记录中继续表示不可变的物理存储命名空间。主项目由任务的 subproject_id 推导。

旧主项目的逻辑 ID 保留原 project_id；新增主项目当前也分配同值的物理命名空间，但所有任务定位都经过 ProjectManager 的映射。子项目名字和 ID 不进入图片路径。重命名和移动只原子替换 catalog.json。任务执行状态继续从现有执行与队列记录推导，不复制保存到逻辑目录。

默认子项目名为“未分类任务”。既有数据迁移需要显式 dry-run、备份、完整性核对和批准；服务不会在导入或启动时偷偷迁移真实数据。测试使用临时数据，真实图片生成不在本轮验收范围。

## 网页接口

沿用 CSRF、ID 格式、错误摘要、上传限制和现有 ArtifactDTO。新页面使用下面的逻辑接口；旧 `/api/projects` 路由继续兼容。

- `GET /api/main-projects` → `{main_projects: [MainOverview]}`。MainOverview 为 MainProject 字段，加 `subproject_count, task_count, image_count, input_count, output_count, counts_by_status, subprojects:[SubprojectOverview]`。image_count 是所有登记输入和输出之和。
- `POST /api/main-projects`，JSON `{request_id,name,description?:""}` → MainOverview。
- `GET /api/main-projects/{mid}` → `{main_project:MainOverview, subprojects:[SubprojectOverview], recent_tasks:[TaskDTO]}`，recent_tasks 最多 8 项，按实际最近更新时间倒序。
- `PATCH /api/main-projects/{mid}`，JSON `{name?,description?,status?}` → MainOverview。
- `POST /api/main-projects/{mid}/subprojects`，JSON `{request_id,name,description?:""}` → SubprojectOverview。
- `PATCH /api/subprojects/{sid}`，JSON `{name?,description?,status?}` → SubprojectOverview。
- `GET /api/subprojects/{sid}/tasks` → `{subproject:SubprojectOverview, main_project:MainProject, tasks:[TaskDTO]}`。
- `POST /api/subprojects/{sid}/tasks`，沿用原 multipart `request_id,operation,prompt,aspect_ratio,base_artifact_id?,files`，另可传 `name,description` → TaskDetail。当前子项目明确传在路由中；不隐式生图。
- `GET /api/tasks/{tid}` → TaskDetail。
- `PATCH /api/tasks/{tid}`，JSON `{name?,description?}` → TaskDetail；不修改历史提示词。
- `POST /api/tasks/{tid}/move`，JSON `{target_subproject_id,expected_subproject_id?}` → TaskDetail。同一主项目限定；当前子项目已变化时拒绝陈旧请求。
- `POST /api/tasks/{tid}/run` 和 `/restore` → 原队列返回值，请求体及明确点击要求与旧路由相同。
- Artifact 预览、审核与下载接口不变。

TaskDTO/TaskDetail 保留已有字段，新增 `main_project_id, subproject_id, main_project_name, subproject_name, name, description, updated_at`。前端用 `name` 做任务标题、保留 prompt 全文。ArtifactDTO 增加 `main_project_id,subproject_id`（按其 task_id 当前归属推导），不返回磁盘路径。`status_label` 优先用于状态文案。

SubprojectOverview 为 Subproject 字段，加 `task_count,image_count,input_count,output_count,counts_by_status`。Main/Sub status 只支持 active/archived；此轮网页需要名称、描述编辑，不要求开发归档操作。

`GET /api/config` 新增 `project_migration_required`。若旧数据尚未批准迁移，旧数据仍可只读投影查询；逻辑写操作拒绝并提示先执行迁移。新建空数据根可以直接使用。开发预览服务使用迁移后的临时数据副本，与真实 7860 服务分开。

## Python 接口

`ProjectManager(storage)` 提供 `create_main_project, get_main_project, list_main_projects, update_main_project, create_subproject, get_subproject, list_subprojects, update_subproject, default_subproject`。

任务归属提供 `create_task(subproject_id, operation, prompt, base_artifact_id=None, *, name=None, description="", task_id=None)`、`get_task_placement(task_id, main_project_id=None)`、`list_tasks(main_project_id=None, subproject_id=None)`、`move_task(task_id,target_subproject_id,expected_subproject_id=None)` 和 `update_task(task_id,name=None,description=None)`。create_task 返回原 Storage Task；list_tasks 返回 `(Task, TaskPlacement)` 对；name/description 位于 TaskPlacement。内部 `reserve_task/finish_task` 为工作台保存流程保留创建归属意图。

迁移接口提供 `migration_plan()`（不修改项目或图片文件）、`backup_metadata(backup_directory)`（仅准备备份）和 `migrate_legacy(dry_run=True, expected_fingerprint=None, backup_directory=None)`（默认仅预览；应用须传 dry_run=False 和匹配的 fingerprint）。结果包含计划摘要、计数、备份路径和校验结果。全部已存在物理 JSON/图片不得因迁移被改写。

`summary(main_project_id=None,subproject_id=None)` 提供域层任务/输入/输出计数和 updated_at；网页的实时执行状态汇总由 WorkbenchState 在该基础上组合。
