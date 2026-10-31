# 图片生成与修改工作台

架构治理增加 Ruff 增量防护与显式脚本插件试点，保留原任务、积分和历史版本路径；审计结论和后续优先级见 [当前架构](docs/architecture/CURRENT_ARCHITECTURE.md)，启停与回滚见 [插件试点](docs/architecture/PLUGIN_PILOT.md)。

这是一个可运行的个人本地图片生成与修改工作台，用于保存项目、任务、参考图、图片输出及修改历史。
支持 PNG、JPEG、WEBP，使用 JSON 保存元数据。第一阶段完成存储，第二阶段新增本地任务执行器和 CLI，第三阶段新增顺序批测，第四阶段接入 FastAPI/Jinja2 网页和独立 Worker。
代码维护现按模块边界组织：全站装配、HTTP 路由、请求契约、业务服务、共享工具和前端组件分开维护。使用 `.\.venv\Scripts\python.exe -X utf8 scripts\verify.py --module video` 可验证单项功能，省略 `--module` 执行完整回归；详见 [模块结构与安全修改指南](docs/architecture.md)。
现已新增主项目 → 子项目 → 任务组织方式。逻辑归属独立保存在 catalog，重命名和移动任务不移动图片。已有数据需先预览、备份并明确应用迁移；详见 [项目层级使用与迁移说明](docs/project_hierarchy_guide.md) 和 [本轮验收报告](reports/project_hierarchy/acceptance.md)。
第二阶段完整命令、零额度测试流程及单次真实生图命令见 [任务执行器使用说明](docs/task_runner_guide.md)。
真实 Codex 图片生成和基于历史图片的编辑均已验收通过，成功产物自动归档。首轮连接超时已通过子进程沿用现有系统代理解决；详见 [最新验收记录](docs/acceptance/2026-10-12-run-02/report.md)。

现已增加账号登录、Workspace 隔离及积分控制：普通新账号 200 积分，纯提示词每张 40，参考图/编辑每张 60。`moy` 为无限额度管理员，已有项目通过元数据映射归入默认空间。项目支持重命名、归档、跨本人 Workspace 移动和回收站恢复，详见 [账号、空间、积分及迁移说明](docs/accounts_workspaces_credits.md)。
图片公开案例模板库仍预留为空，模板由用户后续提供。

现已新增独立的“脚本与剧本”页面（`/scripts`）及 `video-script-generation` Skill：选择主／子项目、脚本或剧本模板、Codex 模型与思考强度，按固定积分生成 UTF-8 TXT；支持历史版本、手动修改和继续生成新版本。两份用户真实剧本已作为格式参考导入，分镜文件仅保留为下游参考。云端文本任务由本机脚本执行器使用既有 Codex 登录执行。模型、价格和模板分别配置，详见 [脚本生成实现与验收](docs/video_script_generation.md)。

图片“创作需求”新增可选的 **AI 强化**：支持标准/创意两种方式，可查看、编辑和对比强化结果；点击“使用强化版”只更新输入文字，仍需点击“开始生成”才保存并提交图片任务。强化不改任务状态、不扣积分、不调用生图后端，默认使用离线 Mock。配置、限制、失败回退与离线验收见 [提示词强化](docs/prompt_enhancement.md)。

网站功能、安全和商业化准备按 [循环优化清单](work/iteration-plan.md) 推进；阶段目标与成本核算见 [商业化准备方案](docs/commercial_readiness.md)。现已加入受控注册、运营容量预算、认证并发限流和一致性备份，修复重试回放及迟到通知回退状态的问题。本轮R01–R11结果、实际备份恢复及数据核对见 [连续优化验收](work/iterations/goal-acceptance.md)。

## 打开网页工作台

新增 **[Qwen 本地模型测试版](https://124.156.182.151/qwen-test)**：管理员可测试 Qwen-Image-2.1-Turbo FP8 / Q5 的文生图、单图编辑和双图参考编辑。需求与原图从网站传到本机 ComfyUI，成图回传后可对照、下载和继续修改；测试版不扣积分。电脑重启后双击项目根目录 `启动Qwen网页测试.cmd` 恢复执行器。详见 [测试页与启动说明](docs/qwen_local_beta.md)。

已新增 [H3 创作工作台](http://127.0.0.1:7860/video)，按“图片 → 脚本 → 视频”组织，每步都有生成和上传入口。图片生成可添加参考图，生成结果可加入项目图库；脚本可生成或上传 TXT 并用于镜头；视频可用 H3 生成或上传 MP4 预览、下载。支持文生视频、首帧／首尾帧、1–9 张参考图生成视频，单镜 4–15 秒。首尾帧和 Picture 标签按勾选顺序排列，取消后重新勾选可调整顺序。使用方式及执行器启动见 [H3 三步创作流程](docs/h3_pipeline.md)。

视频默认开放 720p（40 积分/秒）；仅在网站进程设置 `IMAGE_WORKBENCH_ENABLE_1080P=1` 时开放 1080p（80 积分/秒）。应先在当前显卡和模型环境完成真实生成验证，再开放更高档位。管理员可在“视频积分设置”调整已开放档位的价格。横屏交付尺寸分别为 1280×720 与 1920×1080，竖屏互换宽高，方形分别为 720×720 与 1080×1080。关闭档位后不能新建或重新提交该档分镜，已运行任务与历史结果继续恢复、结算和下载。计费按镜头填写时长 × 该分辨率提交时单价，提交前显示报价；排队冻结，成功扣除，明确失败退回，结果待确认时保持冻结。单价改变不影响已提交镜头。图片和视频共用账号余额，使用各自的执行队列。

本机视频执行器复用已安装的 ComfyUI 和 `D:/ChatGPT/aigc/workflows/minimax_h3` 原生模板，不需要重新下载模型。配置示例是 `deploy/h3-worker.example.json`；复制到 `deploy/h3-worker.local.json` 后运行：

```powershell
# 只读检查服务、节点、模型及视频工具；不提交任务
.\scripts\start_h3_worker.ps1 -ConfigPath .\deploy\h3-worker.local.json -Check
# 执行用户已提交、已冻结积分的本机镜头
.\scripts\start_h3_worker.ps1 -ConfigPath .\deploy\h3-worker.local.json -DataRoot .\data
```

需要先启动现有 ComfyUI（本机项目配置为 `127.0.0.1:8189`）。执行器一次处理一镜，并与同机其他网站视频执行器共用 GPU 锁；ComfyUI 有其他任务时等待。缓存须保留，断线后优先查询同一工作流回执和回传结果，不自动重新生成。高清生成按工作流要求向上对齐到32的倍数，再中心裁去多余边缘至选定尺寸，不把低清结果放大。生成视频经过尺寸、帧率、时长及完整解码检查，单个输出最大 40 MiB。H3 帧数网格可能使实际片长略大于所填时长，积分仍按提交时展示的时长计算。

2026-10-16 本机实测的 4 秒 I2V 目标镜头：720p 用时约 341 秒，1080p 用时约 1293 秒，两档均通过完整解码、精确尺寸和积分结算验证。1080p 在 RTX 5070 12 GB 上使用 `--vram-headroom 2`；本机 `D:/ChatGPT/aigc/scripts/start_comfyui.ps1` 已默认保留 2 GiB 显存，可通过 `-VramHeadroomGiB` 调整。公网已显式开启双档；本地网站重启时需要设置 `$env:IMAGE_WORKBENCH_ENABLE_1080P='1'` 才显示 1080p。

云端接入使用同一个执行器：在独立配置中增加 `server`，设置独立 `cache_root`，启动时通过 `-WorkerConfigPath` 指向已有的受保护执行器配置。令牌不放在命令行参数或源码里。云端程序须先发布支持视频分区的版本，并确认执行器心跳在线。旧版低分辨率分镜需重新创建后才能按高清档位生成；已完成视频保留播放、下载及原积分结算。

管理员可从侧栏进入[管理中心](docs/admin_console.md)，查看服务与运营配置、运行功能检查、查看对应版本的报告，并准备更新包和确认部署。部署按钮使用当前本机连接配置；云端运行目录不持有本机发布凭据。

任务完成或失败后，列表和详情保留本次执行耗时；排队时间单独区分。新增结构化性能记录与离线汇总，说明见[性能测量口径](docs/performance_analysis.md)，本轮经确认的10次真实生成结果见[性能报告](reports/performance-20261013/performance_summary.md)。

已增加“腾讯云收需求、本机 Codex 执行、图片回传”的双端模式。服务器使用 `cloud`，这台电脑使用 `remote-worker`；离线排队、断线续传及独立访问认证已实现。配置和恢复方式见 [云端与本机传递部署说明](docs/remote_deployment.md)。真实腾讯云实例尚待连接验证。

服务器首次配置完成后，可用 `.\scripts\deploy.ps1` 一键检查并同步更新代码；先运行 `.\scripts\deploy.ps1 -PackageOnly` 可仅在本机检查和打包。详细步骤见 [一键同步部署](docs/one_click_deploy.md)。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
.\.venv\Scripts\python.exe -X utf8 -m image_workbench web
```

访问 **http://127.0.0.1:7860**。创建主项目 → 创建/选择子项目 → 上传参考图和填写需求 → 开始生成（点击后立即提交执行）→ 查看预计生成时间、真实三节点状态和结果 → 人工通过 → 下载图片 → 从任意历史输出继续修改。结果面板底部的“本次任务积分”只显示后端计费状态（预计消耗、已冻结、已扣除、已退回、免费或待确认）。主项目总览可编辑项目、查看统计；任务详情可修改显示名称并移动至同一主项目下的其他子项目。

点击结果图可缩放查看；有基底的编辑任务可拖动对比前后变化，异尺寸图片保持原比例并排显示。操作与键盘说明见 [图片查看与对比](docs/image_viewing.md)，第三方固定版本及许可见 [组件说明](docs/third_party_components.md)。查看操作不生成图片、不改变审核状态、不扣积分。

创建任务接口本身只保存草稿；“开始生成”在同一操作中依次提交创建和执行请求，执行仍沿用原队列、幂等和积分事务。右侧预计生成时间只读取已完成的同类任务耗时，样本不足时明确显示“暂无足够历史数据”，不推测百分比。真实生成复用原 Codex 后端；刷新、关闭页面和网页重启不会自动重放任务。第四阶段已通过无额度网页演练，新增网页入口的真实生成/编辑待你手动验收。详见 [中文使用手册](docs/web_guide.md)、[A–D准入与第四阶段验收](reports/phase4_acceptance.md)。

## 安装与验证

存储和执行器需要 Python 3.10 或以上；当前网页依赖已在 Python 3.12 验证。进入本仓库，在 PowerShell 中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用 `unittest`，网页测试另需 `requirements-dev.txt` 中的依赖；素材在 `tests/` 下的随机临时目录中创建、验证和自动清理，不读取或清空真实 `data/`。

2026-10-12 已在 Windows、Python 3.12.10、Pillow 11.3.0 环境实际验收：
项目层级升级共 **93项测试：92通过、1跳过、0失败**，见 [完整检查输出](reports/project_hierarchy/automated_checks.txt)。测试涵盖迁移回滚、项目重命名、跨子项目移动、图片哈希/路径/版本保持及旧执行链路回归。本轮未使用真实生图额度。
第四阶段共 **64项测试：63通过、1跳过、0失败**，见 [完整检查结果](reports/phase4_automated_checks.json)。覆盖原存储/执行器以及新增Web、Worker和幂等行为。
第一阶段的 11 项回归全部通过，包含完整示例、中文数据、迁移、历史保留、损坏图片、越界路径、真实目录 junction 及部分写入失败回滚。
第二、三阶段自动化测试覆盖任务执行、参考图快照、阶段计时、失败分类、超时终止、版本引用、归档恢复、批次重跑、JSON/CSV报告及 CLI 流程；这组测试不调用真实图片模型。
跳过的是 Codex 缓存文件符号链接检查：本机返回 WinError 1314，无创建文件符号链接的权限，因此该项实际文件链接行为未验证。
目录链接测试需要系统允许创建符号链接或 junction；本次目录 junction 检查通过，受限环境可能另行标记跳过。
以上为历史阶段结果。2026-10-13 连续优化的完整后端回归为 **218 项：215 通过、3 项因平台条件跳过**，见 [完整检查输出](work/iterations/goal-release-tests.txt)；之后仅备份脚本及其测试有行为修改，受影响的21项检查全部通过。另在 WSL/Linux 验证了备份恢复与发布回退脚本，未据此宣称全应用已完成 Linux 验收。当前本地依赖已更新且Web/Worker已重启，详见 [依赖复扫记录](work/iterations/dependencies/review.md)。网页写入和 Worker 执行已加入本机进程锁；直接绕过工作台调用 StorageManager 的多进程写入及断电时的跨文件事务仍不在保证范围。真实1267文件已完整备份并隔离恢复，使用方法见 [备份恢复说明](docs/backup_restore.md)。

## 目录说明

```text
图片网站/                         ← 当前仓库就是项目根目录
├── image_workbench/
│   ├── __init__.py              # 公开导入入口
│   ├── config.py                # 根目录与文件/像素限制
│   ├── models.py                # Project、Task、Artifact 数据结构
│   └── storage.py               # 路径校验、复制、JSON、版本关联与查询
│   # 第二阶段新增：image_backend.py、codex_backend.py、task_runner.py、cli.py、__main__.py
├── data/
│   ├── public/template_libraries/ # 公开案例模板库根目录，目前为空
│   ├── users/                    # 个人数据根目录，目前为空
│   └── projects/                 # 第一阶段已有项目存储，继续保留
├── tests/                       # 临时目录内的自动化测试
├── docs/storage_contract.md     # 方法签名、字段、异常和安全边界
├── example.py                   # 使用已有图片的完整调用示例
├── README.md
├── requirements.txt             # 存储/执行器依赖 Pillow
├── requirements-web.txt         # FastAPI/Jinja2/Uvicorn/上传解析
├── requirements-dev.txt         # 网页测试依赖
└── .gitignore
```

`StorageManager()` 会在配置的数据根目录下自动建立上述三个目录入口，重复初始化不会清空内容。
`public/template_libraries/` 目前是多个模板库的空容器，未预设分类，也没有导入案例或创建数据库。
以后每个模板库放在独立的 `<library_id>/` 子目录中；库名、分类和模板内容等素材到位后再确定。

`users/` 是历史预留目录，当前账号隔离不依赖这个目录。账号、Workspace、项目归属和积分账本保存在 `.identity/accounts.sqlite3`；主项目通过 `project_ownership` 映射到所属 Workspace。
图片继续保存在稳定的 `projects/<project_id>/tasks/<task_id>/…` 路径。跨 Workspace 移动项目更新归属映射，保留原文件与 ID；已迁移的历史项目属于 `moy` 默认空间。

目录名称只表达用途，`public/` 目前不会自动发布到网络，`users/` 也不等于已经实现权限隔离。
查看、下载和修改个人图片时，服务端会核对登录身份与 Workspace 归属，图片文件访问同样受保护。
公共模板被选用后应在当前账号下创建独立任务，保留所用模板版本，生成结果默认属于该账号的私人记录。

## 直接运行完整示例

准备三张已有的图片：参考图、第一版结果图、第二版结果图。将下面路径替换为你自己的文件路径：

```powershell
python example.py --reference "D:\素材\参考.png" --output-v1 "D:\素材\第一版.png" --output-v2 "D:\素材\第二版.webp"
```

这会依次创建中文项目和生成任务、复制参考图、登记第一版、查询路径、创建编辑任务、登记第二版、回溯历史。
每次运行创建新项目；三张源图片不会被移动或改写。可以用同一张已有图片验证流程，每次仍会分配独立 ID。
加上 `--data-root "D:\我的工作台数据"` 可将本次数据保存在指定目录。

## 在自己的 Python 脚本中调用

在项目根目录创建脚本，使用下面这些调用即可。文件路径必须替换为实际存在的图片：

```python
from pathlib import Path
from image_workbench import StorageManager

storage = StorageManager()
project = storage.create_project("森林小屋")
task = storage.create_task(project.project_id, "generate", "绘制一座森林小屋")

reference = storage.add_input_image(
    project.project_id, task.task_id, Path(r"D:\素材\参考.png")
)
# 可以重复调用，给同一任务添加多张参考图片。
v1 = storage.register_output_image(
    project.project_id, task.task_id, Path(r"D:\素材\第一版.png")
)
print(storage.get_artifact_path(v1.artifact_id))

edit = storage.create_task(
    project.project_id, "edit", "把天空改为晚霞", base_artifact_id=v1.artifact_id
)
v2 = storage.register_output_image(
    project.project_id, edit.task_id, Path(r"D:\素材\第二版.webp")
)
assert v2.source_artifact_id == v1.artifact_id  # 自动继承编辑任务的基底图
assert storage.get_artifact_path(v1.artifact_id).is_file()  # 旧版本仍然存在
previous = storage.get_artifact(v2.source_artifact_id)
print("前一版:", previous.artifact_id, storage.get_artifact_path(previous.artifact_id))

print(storage.list_projects())
print(storage.get_project(project.project_id))
print(storage.list_tasks(project.project_id))
print(storage.get_task(project.project_id, edit.task_id))
print(storage.list_task_artifacts(project.project_id, task.task_id))
```

`generate` 不接受 `base_artifact_id`；`edit` 必须提供同一项目中已有的图片 ID。
编辑输出默认引用任务的基底图片，显式来源必须与其一致。每次导入均保留独立副本，不按同名或相同内容覆盖。
图片返回格式是冻结的 dataclass，例如通过 `v1.width`、`v1.sha256`、`v1.relative_path` 读取信息。
所有方法及完整字段见 [存储接口约定](docs/storage_contract.md)。

## 配置和数据迁移

```python
from pathlib import Path
from image_workbench import StorageConfig, StorageManager

storage = StorageManager(StorageConfig(
    data_root=Path(r"D:\我的工作台数据"),
    max_image_bytes=10 * 1024 * 1024,
    max_image_pixels=25_000_000,
))
```

也可以在 PowerShell 当前会话设置环境变量：

```powershell
$env:IMAGE_WORKBENCH_DATA_ROOT = "D:\我的工作台数据"
python example.py --reference "D:\素材\参考.png" --output-v1 "D:\素材\第一版.png" --output-v2 "D:\素材\第二版.webp"
```

优先级为显式配置 > 环境变量 > 本仓库 `data/`。默认文件上限 20 MiB、累计帧像素上限 4000 万。
显式相对路径按配置创建时的工作目录解释。图片类型由实际内容判定，文件后缀不参与信任判断。
Pillow 自身的解压缩炸弹保护也保持开启。

迁移时先停止写入，将**整个数据根目录**复制到新位置，保留 `projects/` 以下的全部 JSON 和图片。
然后以新的 `StorageConfig(data_root=...)` 创建管理器。旧 ID、版本关系无需修改；确认新目录可读后自行安排旧数据备份。
不要只复制图片，也不要把查询返回的绝对路径长期保存到业务数据中。

## 错误与边界

- `ValueError`：参数、ID、图片内容、大小、像素或版本关系无效。
- `FileNotFoundError`：资源、图片文件或源文件不存在。
- `MetadataError`：磁盘 JSON 损坏或记录不符合约定；它也是 `ValueError`。
- `OSError`：权限、磁盘写入、复制或原子替换失败。

元数据用 UTF-8 保存，提交使用同目录临时文件及原子替换。复制/验证/提交失败时，既有图片、既有清单和源文件保持不变。
程序只清理本次创建的新文件。受管理路径拒绝符号链接、Windows junction 及越界引用，JSON 中的路径也必须符合规范目录。

StorageManager自身仍要求顺序写入；第四阶段Web通过控制锁和独立Worker串行调用。全局图片查询首次扫描各任务的JSON，后续通过元数据与目录stat签名校验缓存索引；外部变化会重建索引。登录权限、自动哈希巡检和跨文件断电事务尚未实现。
突然断电或进程被强杀可能留下未登记文件，它们不会被查询成有效图片；操作系统的持久化保证不等同于跨文件事务。
如果清理本身也因权限等原因失败，可能残留本次未登记文件，原始异常仍会报告。不要直接编辑 JSON 或修改受管理图片。

## 给初学者的说明

可以把 `StorageManager` 理解成资料管理员：你告诉它要做什么，它负责检查、建立文件夹、复制图片和写入登记册。

| 概念 | 作用 |
| --- | --- |
| 项目 ID | 一个创作主题的编号；中文项目名可以重复，编号不会因此相同 |
| 任务 ID | 一次生成或修改要求的编号；一个项目可以有很多任务 |
| 图片 ID（artifact_id） | 某次保存的图片副本的编号；同一个任务可以有多张图片 |
| source_artifact_id | 新版本记录旧版本的编号，用它可以沿修改历史向前查找 |
| 相对路径 | 只记录图片在数据目录内部的位置；更换数据目录后仍能定位 |

例如登记册保存 `projects/项目ID/tasks/任务ID/outputs/图片ID.png`，管理员将它和当前数据根目录组合为实际路径。
你的业务代码只传 ID，并调用 `get_artifact_path()`，不需要自己拼接文件夹。

## 批量测试

最新 Luna 舰队实测已按用户要求停止，实际保留 **33 张主测图片**，另 1 项主动中断；没有完成原 200 张目标。已修复回复缺路径时的产物恢复、批测持久化进度、图片查询性能、网页缩略图与状态刷新。见 [总结与修复记录](reports/fleet_summary.md)、[33 张结果图册](reports/fleet-200/gallery.html) 和 [批测说明](docs/fleet_guide.md)。停止前最近一轮自动化检查为 76 项：75 通过、1 跳过；原始记录保留。

电商套图清单共16项：纯生成、参考图生成、编辑、异常边界各4项。商品素材和来源保存在 `tests/manual_batch_cases/`。
完整命令、统计口径、失败分类见 [批量测试说明](docs/batch_testing.md)。运行 `python -m image_workbench batch --help` 查看参数。每批报告放在独立的 `reports/` 子目录中，保留历史结果。

第三阶段真实首轮正常任务11/12成功、4个负例全部按预期拒绝；唯一一次编辑因上游未返回图片路径而失败，单独复测后成功，原失败记录保留。平均Codex进程耗时45.86秒，首轮批次9分45秒。另完成代表性真实复跑及两轮相同清单的本地复跑，5个编辑链版本和旧文件哈希均通过核查。详见 [第三阶段验收报告](reports/phase3_acceptance.md) 和 [全部20次真实测试尝试](reports/phase3_all_attempts.csv)。

## 执行器与网页接入

第二阶段的 TaskRunner 已负责读取任务、复制参考图、调用后端、验证产物并交给 `register_output_image()`。
它记录每次执行状态和日志，并支持只恢复归档、不重新生图。继续修改时创建 `edit` 任务并传入基底图片 ID。
第四阶段网页通过独立Worker调用这一层Python接口；HTTP请求不会长时间等待Codex。人工审核、稳定提交ID和队列放在数据根目录的`.workbench/`，项目/任务/图片元数据继续沿用原格式。

`source_path` 只能来自可信本地脚本。网页接收受控上传和图片ID，不接收任意本地磁盘路径；预览和下载只返回合法登记且哈希匹配的图片。
自动化测试没有调用图片模型。真实验收与其分开记录，需用户主动选择启动。本项目使用FastAPI网页与原JSON文件存储，没有新增数据库。
