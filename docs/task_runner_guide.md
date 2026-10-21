# 第二阶段使用说明

已实现本地执行器和命令行。日常使用顺序为：创建任务 → 选择后端执行 → 检查状态 → 查询输出 → 基于输出创建编辑任务。
自动化测试不调用图片模型。2026-10-12 已在用户授权下完成真实生成和编辑，分别用时约 44 秒和 62 秒，均自动归档；详见 [最新验收记录](acceptance/2026-10-12-run-02/report.md)。真实调用需由用户主动选择或明确授权，执行器本身不在超时后自动重试。

## 安装、检查及测试

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python -m image_workbench doctor
python -X utf8 -m unittest discover -s tests -v
```

doctor 只检查版本、help、现有登录状态和功能开关，不调用模型。其 `imagegen_verified: false` 只表示此检查命令不发起生图验证，不代表历史验收没有通过。
本机 CLI、ChatGPT 登录和 ImageGen 技能已找到，真实非交互生图与编辑均已通过。详见 [环境检查记录](codex_environment.md)。
测试使用临时目录、本地生成的测试素材和模拟后端；子进程超时测试仅启动本地 Python 辅助程序，绝不启动真实 Codex 生图。

## 零生图额度的完整流程

下面是 PowerShell 示例，`D:\素材\测试.png` 换成一张已有的本地图片。此处创建的是运行记录和文件副本，不是新画面。
如需指定根目录，将 `--data-root "D:\工作台数据"` 放在每条命令的子命令前；也可设置 IMAGE_WORKBENCH_DATA_ROOT。

```powershell
$p = python -m image_workbench project-create --name "执行器测试项目" | ConvertFrom-Json
$t = python -m image_workbench task-create --project $p.project_id --operation generate --prompt "森林中的小屋" --input "D:\素材\测试.png" | ConvertFrom-Json
$r = python -m image_workbench run --project $p.project_id --task $t.task_id --backend local --image "D:\素材\测试.png" | ConvertFrom-Json

python -m image_workbench status --project $p.project_id --task $t.task_id
python -m image_workbench outputs --project $p.project_id --task $t.task_id

$e = python -m image_workbench task-create --project $p.project_id --operation edit --prompt "把天空改为晚霞" --base $r.output_artifact_ids[0] | ConvertFrom-Json
python -m image_workbench run --project $p.project_id --task $e.task_id --backend local --image "D:\素材\测试.png"
```

可重复 `--input` 添加参考图，重复 `--image` 导入多张已有测试产物。
每次执行分配新的 execution_id、工作目录及图片 ID。编辑任务的旧版本不会被覆盖。
命令失败时先看退出码 `$LASTEXITCODE` 和状态/错误 JSON；失败的真实请求不要直接重复提交。

## 手动启动一次真实生图

以下准备步骤只创建本地记录：

```powershell
$p = python -m image_workbench project-create --name "单次真实生图验收" | ConvertFrom-Json
$t = python -m image_workbench task-create --project $p.project_id --operation generate --prompt "生成一张白色背景上的蓝色陶瓷杯静物照片，无文字" | ConvertFrom-Json
```

**只有你主动执行下面这一条，才会启动真实图片请求并使用现有账号额度：**

```powershell
python -m image_workbench run --project $p.project_id --task $t.task_id --backend codex --allow-image-generation --timeout 300
```

该命令沿用现有 ChatGPT 登录，不读取 API key，不修改全局配置，不自动重试生成。
为保护项目文件，只对此次子进程使用隔离配置和只读沙箱，关闭 shell、插件、hooks 和浏览器等工具。
因为 --ignore-user-config，此次运行不继承用户 config.toml 中的模型/插件等定制；认证仍由原 CODEX_HOME 下的现有登录提供。
此次调用启用 `respect_system_proxy`，沿用操作系统中已经配置的代理，避免独立 CLI 绕开现有代理后 WebSocket 连接超时；不改变操作系统或全局 Codex 设置。本机 CLI 会输出此功能处于开发中的提示，已实测生成和编辑通过。
若这个受限 CLI 会话不能提供内置图片工具，执行器会保存失败原因，不回退到其他 API。

成功后执行：

```powershell
python -m image_workbench outputs --project $p.project_id --task $t.task_id
```

JSON 中的 `path` 是最终归档图片路径，`artifact_id` 可用于下一轮编辑任务的 `--base`。
实际生图是否成功必须以 `status: succeeded` 和登记产物为准，不能只看 Codex 退出码。

## 失败、日志与恢复

所有运行资料保存在：

```text
data/projects/<项目ID>/tasks/<任务ID>/logs/runs/<执行ID>/
├── execution.json            # 状态、候选清单和已登记图片 ID
├── stdout.log                # Codex JSONL 或本地后端日志
├── stderr.log                # 子进程错误及执行器错误
└── work/
    ├── references/           # 本次参考图和编辑基底的快照
    ├── products/             # 本次明确返回的候选图片
    ├── request.json          # 本次任务及图片角色
    ├── command.json          # Codex 实际 argv，不含环境变量或登录令牌
    ├── process.json          # Codex 子进程 PID 与开始时间
    ├── response.schema.json  # 最终回复结构
    └── response.json         # 原始产物路径及返回说明
```

local 后端没有 Codex 专属的 command/process/response 文件。所有阶段都保留输入、已产生的文件和日志。
执行记录使用相对路径；诊断日志可能含当时的绝对路径，迁移后它们只作历史说明。

| status | 下一步 |
| --- | --- |
| execution_failed | 查看 stderr 和 response；检查 CLI/登录/工具能力，不自动再生图 |
| timed_out | 子进程已尝试终止，不自动重试；远端已提交的请求是否取消无法保证 |
| missing_artifact | 检查本次明确声明的路径，退出成功也可能没有图片 |
| invalid_artifact | 图片损坏、路径越界、链接、尺寸/大小超限或内容变化，不能登记 |
| archive_failed | 修复文件系统原因后，对同一次执行 retry-archive |
| succeeded | 用 outputs 查询正式保存路径 |

已有完整候选清单时，可只恢复归档：

```powershell
python -m image_workbench retry-archive --project $p.project_id --task $t.task_id --execution "执行ID"
```

此命令不会调用任何后端。多张图片只登记了一部分时，它会复用已登记图片的 ID，补齐剩余图片。
若状态写入时持续发生磁盘故障，命令会报错；修复后磁盘记录可能仍为 running/archiving。
确认旧进程确实已退出后，可用 `mark-interrupted --project ... --task ... --execution ...` 标记，再恢复归档。此命令只改记录，不替代操作系统的进程终止。

如果候选文件尚未收集成功，retry-archive 会拒绝空清单。原始产物路径保留在 response.json，可信本地脚本可用 local 后端和明确的 `--image` 路径另行归档，不需要重新生成。
外部传入的本地路径仅限可信脚本使用，未来网页不可直接透传这些路径。

## Python 初学者需要理解的代码

```python
from image_workbench import StorageManager, TaskRunner, LocalFileBackend

storage = StorageManager()
project = storage.create_project("Python 执行示例")
task = storage.create_task(project.project_id, "generate", "森林小屋")

runner = TaskRunner(storage)
result = runner.run(
    project.project_id,
    task.task_id,
    LocalFileBackend([r"D:\素材\测试.png"]),
)
if result.status == "succeeded":
    print(storage.get_artifact_path(result.output_artifact_ids[0]))
else:
    print(result.status, result.error)
```

- `StorageManager` 管正式保存的项目、任务和图片，继续复用第一阶段代码。
- `TaskRunner` 管一次执行：复制参考图、调用后端、验证图片、登记输出、保存状态。
- `ImageBackend` 是后端要遵守的约定：接收 BackendRequest，返回 BackendResult。
- `LocalFileBackend` 复制现有图片，用来练习和测试流程，不会生成新画面。
- `CodexImageBackend` 把调用约定转换为安全的 subprocess 参数，收集当前请求明确返回的文件。
- 任务 ID 表示一份创作需求；执行 ID 表示该任务的一次尝试；图片 ID 表示成功保存的一个版本。

`subprocess.Popen([...], shell=False)` 使用参数列表启动程序，prompt 通过 stdin 输入，所以图片需求里的引号或命令符号不会被拼进 shell 命令。
返回值先变成候选文件，经完整解码和限额检查，再登记为正式图片。

## 后续个人网页接入

网页业务层应调用 TaskRunner 和 StorageManager，不依赖 Codex 命令字符串。
账号、资源归属和授权检查需要先于网页开放；目前目录预留不等于已经实现用户隔离。
耗时任务以后可放进单独 worker，网页只提交任务和查询执行状态；本阶段仍限制单进程顺序写入，没有开发队列或网页。
完整字段、异常和协议见 [执行接口约定](execution_contract.md)。
