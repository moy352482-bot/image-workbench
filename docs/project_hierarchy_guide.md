# 主项目、子项目与存储分离

项目按主项目 → 子项目 → 任务 → 图片版本组织，只支持两级项目。网页在主项目总览提供子项目创建、名称与描述编辑、任务/图片统计及最近任务；子项目工作区保留上传、草稿保存、明确点击执行、审核、下载和从历史图片继续修改。移动任务时会显示目标子项目；同一主项目内允许移动，跨主项目拒绝。

## 数据事实来源

| 模型或记录 | 唯一职责与字段 |
| --- | --- |
| MainProject | main_project_id、name、description、status、created_at、updated_at |
| Subproject | subproject_id、main_project_id、name、description、status、created_at、updated_at |
| TaskPlacement | task_id、storage_project_id、subproject_id、显示名称/描述、时间、pending/ready 提交状态 |
| 原 Task | 保留 operation、prompt、base_artifact_id 等原始任务字段；project_id 指物理命名空间 |
| 原 Artifact | 保留 artifact_id、task_id、kind、相对路径、哈希、source_artifact_id 等全部原字段 |

前三类记录仅保存在 `data/.project_management/catalog.json`。任务只保存一份 subproject_id；主项目通过子项目推导，不再维护主项目或子项目的 task_ids 清单。任务执行状态、输入、输出和审核状态仍从原有权威记录组合查询，不在 catalog 复制。

`storage_namespaces` 将逻辑主项目映射到不可变的物理命名空间。旧主项目 ID 保留原 project_id，当前新主项目也分配同值，但调用方必须经过 ProjectManager 映射，不能据此自行拼路径。`TaskPlacement.storage_project_id` 只做物理定位；名称和子项目 ID 不进入图片路径。

`StorageManager` 继续是图片读取、写入、登记、哈希检查与历史回溯的唯一入口。`TaskRunner`、`CodexImageBackend`、Artifact 和旧 Task 数据结构没有为层级升级而改写。旧 CLI、批次及 StorageManager 直接创建的记录通过兼容视图落入默认“未分类任务”；已有逻辑重命名和任务移动不会被兼容读取覆盖。网页旧 `/api/projects` 路由保留，新网页使用 `/api/main-projects`、`/api/subprojects` 和全局 task_id 路由。

## Python 使用

```python
from image_workbench import StorageManager, ProjectManager

storage = StorageManager()
projects = ProjectManager(storage)
main = projects.create_main_project("福泽 IP 系列", "角色与宣传物料")
design = projects.create_subproject(main.main_project_id, "人物形象设计")
poster = projects.create_subproject(main.main_project_id, "中秋主题海报")
task = projects.create_task(design.subproject_id, "generate", "人物三视图", name="人物三视图")
projects.move_task(task.task_id, poster.subproject_id,
                   expected_subproject_id=design.subproject_id)
projects.update_main_project(main.main_project_id, name="福泽 IP")
summary = projects.summary(main_project_id=main.main_project_id)
```

上述分类操作不会执行模型。实际生成仍由原 TaskRunner 或网页手动提交。图片修改通过 base_artifact_id 引用同一主项目内的任意历史输出，跨子项目来源保留，移动之后不重写来源 ID。重命名和移动均只原子替换 catalog；文件内容和相对路径不变。整体迁移数据根时，连同 `.project_management`、`.workbench` 和 `projects` 一起复制，并修改 `--data-root` 或 `IMAGE_WORKBENCH_DATA_ROOT`。

## 旧数据迁移

命令默认仅预览，不自动迁移，不调用生图：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m image_workbench --data-root data projects-migrate
.\.venv\Scripts\python.exe -X utf8 -m image_workbench --data-root data projects-migrate --backup-only --backup-directory reports/project_hierarchy/metadata-backups
```

审阅后明确批准，才执行下面的应用命令，指纹替换为本次预览输出的完整值：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m image_workbench --data-root data projects-migrate --apply --expected-fingerprint <完整指纹> --backup-directory reports/project_hierarchy/metadata-backups
```

迁移为每个旧项目建立同 ID 主项目和稳定 ID 的默认子项目，把旧任务关联到默认子项目。所有原 task_id、artifact_id、路径和版本引用保持不变。应用前停止旧网页和所有会写入此数据根的脚本，确认没有在执行的任务；应用完成后再启动新版网页。应用函数也会取得工作台控制锁、执行锁和 catalog 锁，重新核对指纹；数据变化时拒绝应用，需重新预览和确认。

备份在数据根外独立目录保存原有 `projects/**/*.json`、`.workbench/**/*.json`、此前 catalog（若存在），并保存图片哈希/路径/来源清单。图片不移动、不改写，因此此次备份为元数据备份，不是额外的图片全量备份。应用时还会重新备份一次，并核对所有登记图片内容和原有 JSON。唯一提交点是 catalog 的原子替换，避免逐个旧文件修改产生半迁移状态。

异常会恢复此前 catalog 字节；此前无 catalog 时仅移除本次新建 catalog，保存失败结果。成功结果报告落盘失败也回滚。重复迁移返回 unchanged；断电后重新 dry-run 可确认 catalog 是提交前或提交后的完整版本。操作系统断电导致的底层磁盘损坏不属于应用级事务保证。不要在已有新分类或新任务后直接覆盖旧 catalog；先保留当前数据并核对备份差异。

## 修改范围与后续边界

- 新增 `image_workbench/project_manager.py`：模型、归属、统计、迁移、备份和回滚。
- 修改 `workbench_state.py`、`web.py`、`cli.py`、`__init__.py`：逻辑映射、兼容接口、迁移命令及公开导出。
- 修改现有 `web_static/workbench.js`、`web_static/workbench.css` 和 `web_templates/index.html`，保留原网页技术栈。
- 新增域层、Web、CLI 测试及 `scripts/project_hierarchy_rehearsal.py` 副本演练脚本。

当前不支持跨主项目移动、无限嵌套、子项目换主项目、多人权限或删除项目。catalog 全量读取和原子替换适合本地个人工作台；规模继续增长时可在保留稳定 ID 和物理路径契约的前提下迁至数据库。直接绕开 ProjectManager/工作台的外部写入不受 catalog 锁协调，迁移时必须停写。

验收结果见 [本轮报告](../reports/project_hierarchy/acceptance.md)；完整字段与路由见 [接口契约](project_hierarchy_contract.md)。
