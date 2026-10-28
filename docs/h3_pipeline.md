# H3 三步创作流程

`/video` 将同一视频项目组织为“图片 → 脚本 → 视频”，每步都有“生成”和“上传”两个入口。默认第一页直接显示上传／生成参考图，无需先填项目表单；首次上传或准备生成时自动保存项目，名称可选填。通过步骤导航或上／下一步切换页面，保留素材和填写内容。

主页面的输出设置只有 720p／1080p 分辨率选择和只读报价，沿用运营方预设的每秒积分。网页不提供价格输入、保存价格按钮或改价接口；用户不能修改价格，管理员登录该页面也不会出现改价表单。服务端仍重新计算并校验每次提交的报价。

## 图片

拖拽、粘贴或选择文件会直接选入本次任务参考图；图片步骤最多使用 8 张，视频步骤按当前模式限制数量。页面仅显示当前任务已选图片，每张提供“移除”按钮；移除取消本次选择，不删除已归档素材或已保存镜头引用。素材暂存区、项目参考图库和已有图片选择入口在此界面隐藏。生成结果可通过“加入参考图”直接用于本次生图，或通过“用于视频生成”加入当前镜头。原有归档、权限、幂等及恢复接口保持兼容，无需数据迁移；回滚仅需恢复前端文件。

## 脚本

选择脚本／剧本、模板、模型、思考强度，填写需求并获取报价后生成 TXT。沿用固定价格和未定价档位拦截，不自动替换手动选择的模型。“上传 TXT”接受 UTF-8 文本（不超过 256 KiB），建立不可变 Task、Artifact 和版本，不收生成积分。

生成或上传的 TXT 可设为项目脚本，用于镜头提示；“打开完整脚本编辑器”定位到对应主／子项目，提供历史版本和修订。图片任务与脚本任务由内部项目映射归属到当前账号、空间和视频项目，不能跨项目取用结果。

## 视频

“H3 生成”保留既有逐镜控制，包括生成模式、参考图、时长、分辨率、动作和声音要求。生成前仍展示报价并确认提交。“上传 MP4”接受不超过 40 MiB 的成品，提供播放与原文件下载，不调用 H3、不收生成积分。上传成品不能误提交为生成作业。

网页工作流分别提供“参考素材生视频”“首帧生视频”“首尾帧生视频”“文生视频”。首帧只使用 1 张图；首尾帧必须选择 2 张，按勾选顺序标记首帧和尾帧。切换模式保留当前选择，数量不符时提示调整，保存前校验。素材菜单“设为首帧／尾帧”和 AI 分镜带入会同步选择对应模式，历史镜头也按实际参考图数量显示名称。

这次选项拆分属于 Frontend，复用现有视频和分镜公开接口，不新增 Core、数据模型或服务依赖。网页 `FL2V` 提交为既有 `I2V` 加两张有序参考图，后端磁盘格式、Workspace 权限、积分、执行和恢复流程不变，无数据迁移；回滚前端文件即可恢复合并选项。验证覆盖浏览器选项、数量约束、保存与重载、素材快捷操作、分镜引用回填及视频／分镜模块检查。

## 云端与本机执行器

### 内置 H3 Skill 与 Luna Fast

`skills/h3-video-workflow/SKILL.md` 封装本站现有 T2V、I2V、R2V 单镜工作流。启动 `h3-worker` 后，新领取的任务在参考图和参数验证完成后，先调用 Luna 整理提示词，再由原来的 ComfyUI 工作流生成视频，无需用户另行选择 Skill。上传 MP4 不经过此调用。

Skill 内置 MiniMax 官方 `h3-prompt-writing` 的基础和全参考指南，固定来源版本 `d21241f0a4b3acbb34c97dae47fa417b7065e438`，正文原样保存在 `references/base-en.txt`、`references/ref-en.txt`，附来源、哈希、许可和 NOTICE。T2V 对应 T2VA；一张图的 I2V 对应 I2VA；两张首尾帧图的 I2V 对应 FL2VA；R2V 对应 Ref2VA。执行器将适用指南正文随 Skill 直接传给 Luna，R2V 同时使用基础指南的通用表达规则。运行时不从网络拉取指南。

外层仍为既有任务 JSON，内部 `prompt` 改为官方三段或六段的多行文本；新请求校验字段是否齐全、顺序正确及内容非空。提示词描述使用英文，用户对白、歌词和画面文字保留原语言。参考图顺序、时长、分辨率和执行事务不变。当前文本调用不查看图片像素，因此只根据用户文字与图片标识表达参考关系，不补造未知的外观细节。

`config/h3_workflow.json` 固定 `model_id: gpt-6-luna`、`service_tier: fast`，并独立配置 `thinking_level: low`、180 秒超时。CLI 明确传入 `--model gpt-6-luna -c 'service_tier="fast"'`，继续使用本机 Codex ChatGPT 登录，不新增 API Key。Fast 是服务档位；按 [Codex 官方配置说明](https://learn.chatgpt.com/docs/config-file/config-reference)，请求映射为 `priority`。账号可用性与服务端实际处理档位须以真实调用为准，本地 `-Check` 只报告配置，不调用 Luna。

Luna 只能返回固定 JSON，整理视频提示词；模式、时长、分辨率、画幅、尺寸和参考图顺序必须与已确认输入完全一致。输出不合法、文本调用明确失败时，不提交 ComfyUI，由现有失败流程释放视频冻结积分；不自动切换模型、服务档位或退回原提示词渲染。

本机任务 `journal.json` 保存完整调用输入（含 Skill 正文）、Skill SHA-256、模型配置和验证后的计划；`skill/` 保存调用意图、命令、日志和完成凭据。重启只恢复校验通过的同一次文本结果，不重新请求 Luna。文本完成状态不明时，服务端保留已领取的运行任务和冻结积分，执行器返回 `uncertain`，在 `skill_error` 记录原因；只等待原完成凭据或人工核对，不提交视频。ComfyUI 提交后的恢复机制不变。

已有检查点没有 `skill_request` 时保持原执行路径；已生成的工作流不重新整理提示词。无数据迁移、HTTP 或积分报价变更。回滚代码前应完成或保留带 Skill 的未完成任务及原执行器版本；不能用旧执行器接管尚未生成 `skill_plan` 的任务，也不要删除缓存。

架构归属为 **Skill + Video Workflow**。`h3_skill` 属于 `video`，仅依赖基础原子文件和错误契约；文本 Provider 在 CLI 装配，通过 `generate/recover` 端口注入，视频模块不导入脚本模块。复用现有 Codex 文本适配器、`H3Worker`、`ComfyH3Adapter`、存储与积分事务，不扩展共享 Core 的业务逻辑。只增加本地可选检查点字段，不改变 Workspace、素材归属、正式产物注册或历史版本。验证覆盖 Mock 模型参数、三种模式、不可变输入、完成凭据恢复、未完成调用禁止重试、ComfyUI 对账、结算及发布包资源；使用 `scripts/verify.py --module video` 和受影响的文本适配器、发布包测试。

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
