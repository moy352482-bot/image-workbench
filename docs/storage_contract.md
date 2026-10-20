# 第一阶段存储接口约定

本文件在测试编写前确定接口。项目根目录就是当前仓库，不再嵌套同名项目。
要求 Python 3.10+、Pillow；仅支持单进程、顺序调用。本页描述存储层，无数据库或网页；第二阶段执行器见 [执行接口约定](execution_contract.md)。

## 配置和导入

```python
from image_workbench import StorageConfig, StorageManager, MetadataError

config = StorageConfig(
    data_root=None,                   # None：读取环境变量，否则使用仓库内 data/
    max_image_bytes=20 * 1024 * 1024, # 默认 20 MiB，必须为正整数
    max_image_pixels=40_000_000,      # 解码像素上限，必须为正整数
)
storage = StorageManager(config)     # 也可直接 StorageManager()
```

`IMAGE_WORKBENCH_DATA_ROOT` 在创建配置时读取，显式 `data_root` 优先。
显式或环境变量相对路径相对于配置创建时的工作目录，随后解析为绝对路径。
默认目录相对于 `config.py` 所在项目，与运行时工作目录无关。
数据根目录是可信配置；源路径只能由可信本地脚本提供。

初始化会建立 `projects/`、`public/template_libraries/`、`users/` 三个目录入口，
使用同一套安全路径检查，已存在的内容不清空。后两者只是空目录预留，不改变下列项目/任务接口。

## 固定方法签名

返回值是冻结的 dataclass，可通过属性读取；列表按 `(created_at, ID)` 排序。

```python
create_project(name: str) -> Project
get_project(project_id: str) -> Project
list_projects() -> list[Project]

create_task(project_id: str, operation: str, prompt: str,
            base_artifact_id: str | None = None) -> Task
get_task(project_id: str, task_id: str) -> Task
list_tasks(project_id: str) -> list[Task]

add_input_image(project_id: str, task_id: str, source_path: str | Path,
                role: str = "reference") -> Artifact
register_output_image(project_id: str, task_id: str, source_path: str | Path,
                      source_artifact_id: str | None = None,
                      *, artifact_id: str | None = None) -> Artifact
get_artifact(artifact_id: str) -> Artifact
get_artifact_path(artifact_id: str) -> Path
list_task_artifacts(project_id: str, task_id: str) -> list[Artifact]
```

- ID 是内部产生的 32 位小写十六进制 UUID，不接受任意名称、绝对路径、斜杠、`..` 或 Windows ADS。
- `name`、`prompt`、`role` 必须是非空白字符串，保留中文和原文；名称仅为元数据，不参与路径构造。
- `operation` 只接受 `generate` / `edit`。`generate` 不允许 base；`edit` 必须传入已存在、同项目的 base（输入图或输出图均可）。
- 输出总是复制，不移动源文件。输出的 `source_artifact_id` 必须属于同一项目且文件仍存在。编辑任务未显式传入来源时使用 task.base_artifact_id；显式来源必须与 base 一致。
- 输入图 `kind="input"`、`role` 默认 `reference`、来源为 `None`；输出图 `kind="output"`、`role=None`。
- 每次成功导入均产生新的 artifact_id，即使文件同名或内容相同，也不去重、不覆盖。
- 第二阶段执行器可通过仅关键字参数 `artifact_id` 提交预分配的 UUID，以恢复中断的归档。该 ID 仍须合法且未占用，已有记录绝不覆盖；普通调用不需要传入。
- 根据真实内容接受 PNG、JPEG、WEBP，按真实格式分配 `.png`、`.jpg`、`.webp`。验证文件结构并完整解码所有帧；累计帧像素不得超过配置上限。扩展名不决定文件类型，不转换图片内容。
- 全局图片查询遍历项目/任务清单，不另维护容易失配的全局索引。本阶段适用于个人数据量。

## 数据结构及磁盘约定

`Project`: `project_id, name, created_at`。

`Task`: `task_id, project_id, operation, prompt, base_artifact_id, created_at`。

`Artifact`: `artifact_id, project_id, task_id, kind, role, original_filename, format,
width, height, size_bytes, sha256, relative_path, created_at, source_artifact_id`。

时间为带时区的 UTC ISO 8601 字符串。相对路径从数据根目录开始，统一使用 `/`。
JSON 使用 UTF-8、`ensure_ascii=False`，带 `schema_version: 1`：project.json/task.json
在对象中添加版本字段，artifacts.json 为 `{"schema_version": 1, "artifacts": [...]}`。

```text
data/projects/<project_id>/
  project.json
  tasks/<task_id>/
    task.json
    artifacts.json
    inputs/<artifact_id>.<真实格式扩展名>
    outputs/<artifact_id>.<真实格式扩展名>
    logs/
```

整体移动数据根目录（包括全部 JSON 与图片），以新根目录创建管理器，原 ID 和关系继续有效。
调用方通过 `get_artifact_path` 获取当前真实路径，不持久保存返回的绝对路径。

## 后续模板与账号目录预留

用户已确认：先建立空白案例库，模板以后提供，当前继续只做目录和本地存储。

实际建立的目录为：

```text
data/
  public/template_libraries/    # 空容器，未来按 library_id 分库
  users/                        # 空容器，未来按 user_id 分个人数据
  projects/                     # 现有单人存储接口继续使用
```

未来个人项目的规划路径为 `users/<user_id>/projects/<project_id>/`，当前尚未实现账号目录创建接口。
不可把现有全局 `list_projects()` / `get_artifact()` 直接当成按账号过滤的接口。
当前元数据无 `owner_user_id`，旧项目不会自动迁移或分配归属；新增账号数据层时需要明确映射和迁移规则。
公开模板与个人项目分别管理；使用模板创建个人任务时，需要记录模板及版本来源，私人输出不自动成为公开模板。
`public` / `users` 的目录区分不代表已经实现网站发布、登录或访问控制。

## 失败与安全边界

- 参数、ID、越界路径、图片损坏/不支持格式、超出限制、非法版本关系：`ValueError`。
- 正确格式但不存在的资源、源文件丢失或登记后图片丢失：`FileNotFoundError`。
- JSON 损坏、字段不符合约定、记录 ID 与目录不匹配：`MetadataError`（`ValueError` 子类）。损坏记录不会静默忽略。
- 权限、磁盘、复制与替换等文件系统故障：保留 `OSError`，调用方可捕获。失败清理不得掩盖原始异常。
- 受管理路径逐段检查，拒绝符号链接及 Windows reparse point/junction；核对解析后仍在根目录内。元数据中的路径还必须与其项目、任务、种类、ID、真实格式组成的规范路径完全相同。
- 不接受数据根目录之外的受管理资源引用。允许可信脚本读取外部 `source_path`，仅用于导入副本；未来网页只能提交经过授权的资源 ID，上传落盘入口另行实现。
- 图片先有限量地复制到目标目录临时文件，完成哈希、大小和内容验证，再以独占创建写入最终文件。清单通过同目录临时文件、flush/fsync 和原子替换提交。提交失败时仅清理本次创建的新文件，不删除/移动源文件及既有版本。
- 项目和任务先在隐藏临时目录内完整建立，再改名发布。正常操作异常时清理本次临时目录；未发布目录不列为项目/任务。
- 图片和清单无法形成跨文件系统事务。突然断电/进程被强杀可能留下未登记图片或隐藏临时文件；查询只信任已提交清单，不会把这些当成有效资源。本阶段不实现自动清理，不承诺断电后的完整持久性。
- 不防御拥有同等本地写权限的进程在检查和使用之间恶意更换文件（TOCTOU），也不支持多进程并发写入；不提供公网 API 或权限系统。
- 获取图片路径会校验路径和文件存在，但不会每次重新解码/校验内容哈希；直接在磁盘外部改写图片属于本阶段的管理边界之外。
