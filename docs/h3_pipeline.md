# H3 三步创作流程

`/video` 将同一视频项目组织为“图片 → 脚本 → 视频”，每步都有“生成”和“上传”两个入口。默认第一页直接显示上传／生成参考图，无需先填项目表单；首次上传或准备生成时自动保存项目，名称可选填。通过步骤导航或上／下一步切换页面，保留素材和填写内容。

主页面的输出设置只有 720p／1080p 分辨率选择和只读报价，沿用运营方预设的每秒积分。网页不提供价格输入、保存价格按钮或改价接口；用户不能修改价格，管理员登录该页面也不会出现改价表单。服务端仍重新计算并校验每次提交的报价。

## 图片

“生成图片”内可直接选择参考图文件，或勾选项目图库。填写画面要求后创建图片任务，再按显示的积分提交现有 Codex 生图队列。最多使用 8 张参考图；结果可预览并加入项目参考图库。“上传图片”支持将已有图片直接加入图库。图库供后续 H3 镜头选择，重复加入同一成品会去重。

## 脚本

选择脚本／剧本、模板、模型、思考强度，填写需求并获取报价后生成 TXT。沿用固定价格和未定价档位拦截，不自动替换手动选择的模型。“上传 TXT”接受 UTF-8 文本（不超过 256 KiB），建立不可变 Task、Artifact 和版本，不收生成积分。

生成或上传的 TXT 可设为项目脚本，用于镜头提示；“打开完整脚本编辑器”定位到对应主／子项目，提供历史版本和修订。图片任务与脚本任务由内部项目映射归属到当前账号、空间和视频项目，不能跨项目取用结果。

## 视频

“H3 生成”保留既有逐镜控制，包括生成模式、参考图、时长、分辨率、动作和声音要求。生成前仍展示报价并确认提交。“上传 MP4”接受不超过 40 MiB 的成品，提供播放与原文件下载，不调用 H3、不收生成积分。上传成品不能误提交为生成作业。

## 云端与本机执行器

网站保存队列、文件和积分，本机分别运行图片、脚本和 H3 执行器。三者使用现有私有执行器凭据；文本执行不新增 API Key。脚本模型目录取自本机实际 Codex 环境，并通过心跳同步给云端。

在项目目录分别启动，保持各终端运行：

```powershell
.\scripts\start_remote_worker.ps1 -ConfigPath "$env:LOCALAPPDATA\ImageWorkbench\Tencent\remote-worker.json"
.\.venv\Scripts\python.exe -m image_workbench.script_remote_worker --config "$env:LOCALAPPDATA\ImageWorkbench\Tencent\remote-worker.json"
.\scripts\start_h3_worker.ps1 -ConfigPath .\deploy\h3-worker.remote.json -WorkerConfigPath "$env:LOCALAPPDATA\ImageWorkbench\Tencent\remote-worker.json"
```

H3 还需要本机 ComfyUI 就绪，可先运行已有 `D:/ChatGPT/aigc/scripts/start_comfyui.ps1`，再运行 H3 启动命令加 `-Check` 核实节点、模型和视频编码器。公网任务依赖这台电脑联网且相关执行器运行，目前没有新增开机自启动。

脚本执行器在网络中断后重传同一完成结果，不重新推理。原执行状态不明时保留检查点和冻结积分；只有人工确认旧执行已停止，才可停止脚本执行器并执行：

```powershell
.\.venv\Scripts\python.exe -m image_workbench.script_remote_worker --config "$env:LOCALAPPDATA\ImageWorkbench\Tencent\remote-worker.json" --confirm-stopped <任务ID> --once
```

该命令不杀进程，不重跑生成；确认结果回传后可以重新启动常驻执行器。

## 验证证据

2026-10-16 入口修订版 `20261016T134559Z-edaa810d` 已发布。第一页无项目时直接上传或生成参考图、三步切换、1080p 选择保留均经独立浏览器环境验证；本轮未额外调用真实模型。完整回归 420 项，416 通过、4 项环境条件跳过。线上核实预设价格仍为 720p 每秒 40 积分、1080p 每秒 80 积分，改价请求返回 405，服务 active，全部 87 个运行文件与受检版本一致。证据位于 `work/h3-reference-entry/`，检查记录位于 `.deploy/20261016T134559Z-edaa810d/`。

最终完整本地回归共 420 项，416 通过、4 项环境条件跳过。新增覆盖三步导入、项目隔离、图片成品复用、TXT 版本、MP4 原文件下载、远端模型目录、定价与积分、断线重传、完成凭据恢复、不明状态禁止重跑及 Windows 缓存根目录迁移。浏览器验收使用隔离测试数据和模拟生成；实际选择文件、切换入口、下载和 MP4 解码均通过。

2026-10-16 已发布版本 `20261016T092916Z-48e94adb`。云端真实验收完成带参考图的 Codex 生图、Luna High 脚本生成、三类上传，以及生成图片和脚本进入 H3 镜头草稿；云端下载 hash 与本机执行器凭据一致。图片首次执行发现缓存根目录已迁往 D 盘的目录联接，修复固定根路径绑定后重试成功；首次失败已退款，最终使用测试账号 60 积分、冻结为 0。仍拒绝缓存内部链接、路径越界和执行中根目录变更。

H3 本轮检查执行器在线、节点／模型与编码工具就绪、720p／1080p 可用及镜头输入衔接，未再次渲染视频。既有 720p／1080p 真实渲染证据位于 `reports/h3-integration-20261016/`。

本地日志、真实图片／TXT 和截图位于 `work/h3-pipeline-release/`；最终发布清单和测试结果位于 `.deploy/20261016T092916Z-48e94adb/`。[最终线上校验](../work/h3-pipeline-release/final-verification.json) 核实服务 active、全部 87 个运行文件与本机受检版本一致。[发布前数据备份日志](../work/h3-pipeline-release/remote-backup.log) 记录完整业务数据备份及校验。
