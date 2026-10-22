# 腾讯云当前部署

最新运行核验（2026-10-22T15:31:26+08:00）：`20261022T071917Z-9998705b`，服务 active/running。每次图片生成和编辑默认应用图片 Skill，并提供 8 种风格的效果预览与案例提示词。发布前合入当时线上 `20261022T070208Z-4635fa2d` 对应的 `e6fd41a`，保留脚本、分镜和 H3 创作 Skill 及已有架构修复；发布源码为 `codex/agent` 的 `9998705`。193 个运行文件逐项 SHA-256 与受检发布包一致，默认图片 Skill 和 8 种风格加载正常，公网 HTTPS 登录、认证配置、风格脚本/样式及 8 张预览图均通过核验，未登录的风格接口返回 401。

发布前完整检查共 718 项，4 项因 Windows 平台条件跳过；WSL 中另外运行的 15 项部署测试全部通过，包含完整运行包解压、版本切换和失败回退。业务数据与服务器凭据未复制或替换。本次发布工作区保留 `.deploy/20261022T071917Z-9998705b/` 的检查、发布包和校验清单，以及 [线上核验报告](../work/image-generation-skill/live-after-release.json)。功能说明见 [图片默认 Skill 与风格案例](image_generation_skill.md)。

此前运行核验（2026-10-20T19:46:51+08:00）：`20261020T110008Z-e3ef56a0`，服务 active。包括图片粘贴、素材暂存区、跨模块引用、脚本自动积分与直接生成功能；151 个运行文件逐项 SHA-256 与本地一致。公网 HTTPS 登录入口、认证配置和新增脚本均返回 200。本次核查发现该版本已在线，没有重复部署、重启或触发生成。证据：[Asset UX 线上核验](goals/asset-ux/LIVE_VERIFICATION.json)。

网站入口：[https://124.156.182.151](https://124.156.182.151)。服务器为 Ubuntu 24.04，SSH 使用 `ubuntu`、22 端口及本机已有专用密钥。`imageworkbench.com` 尚未购买，不用于当前部署。

2026-10-16 最新发布核验为 `20261016T134559Z-edaa810d`：[H3 创作工作台](https://124.156.182.151/video) 第一页直接上传或生成参考图，首次操作自动保存项目；主页面选择 720p／1080p，价格只读，移除网页改价入口。原定价保留为 40／80 积分每秒。最终检查 420 项，416 通过、4 项环境条件跳过；87 个线上运行文件与受检版本一致，服务正常。三步流程此前的真实生成验收及本轮页面、价格权限验收见 [流程与证据](h3_pipeline.md)。下文首次发布记录保留作为历史，不代表后续当前状态。

首次发布版本：`20261013T074038Z-4b4191bf`。因为本地其他工作还在修改代码，本次从固定副本完成检查并发布，发布包与受检程序哈希一致。194 项测试中 191 项通过、3 项因平台条件跳过；检查记录位于 `.deploy/20261013T074038Z-4b4191bf/checks.txt`。较深的 Windows 检查路径触发过路径长度限制，该次失败记录单独保留，换短路径后同一份代码检查通过。

## 首次登录

云端管理员 `moy` 已启用，2026-10-13 已按用户指定的固定密码重置并验证。直接打开网站的“登录”页，输入 `moy` 和该密码，即进入无限积分的管理员工作台。无需再填写启用凭据，也不需要注册。密码不保存在本文档或源码中。

本机 `C:\Users\win\AppData\Local\ImageWorkbench\Tencent\管理员首次启用.txt` 现为登录说明；原一次性启用凭据已经失效。管理员登录验证记录为 `reports/tencent_admin_login.json`，工作台和管理中心均返回 HTTP 200。

云端使用独立账号和业务数据目录。本次未上传本地历史账号库、项目和图片。注册采用邀请码，普通账号上限 100；管理员启用凭据与执行器令牌均不写入此文档或源码库。

## 本机执行器

服务器保存任务，本机分别使用图片、脚本和 H3 执行器生成并回传结果。电脑需要开机联网，相关执行器需要运行；H3 还依赖本机 ComfyUI。网站服务和各执行器是独立进程。三类启动命令见 [H3 三步流程](h3_pipeline.md)。

配置与日志位于 `C:\Users\win\AppData\Local\ImageWorkbench\Tencent\`，目录权限限定为当前 Windows 用户和 SYSTEM。`remote-worker.json` 包含服务器地址、独立执行器令牌和缓存目录；不要公开此文件。管理员启用文件也位于这个目录。

重启电脑或停止执行器后，在项目目录运行：

```powershell
cd "D:\ChatGPT\图片网站"
.\scripts\start_remote_worker.ps1 -ConfigPath "$env:LOCALAPPDATA\ImageWorkbench\Tencent\remote-worker.json"
```

该命令在当前终端运行，保持窗口开启；Ctrl+C 可停止。2026-10-16 发布时已在后台启动三个执行器，进程及日志位置记录在该目录下的 `pipeline-worker-processes.json`；旧 `worker-process.json` 和旧日志保留为历史证据。未设置 Windows 开机自启动。队列及回传都有持久检查点；出现“状态待确认”时先核实原生成进程，不能盲目重新提交。

## 后续发布

```powershell
cd "D:\ChatGPT\图片网站"
.\scripts\deploy.ps1
```

代码保存完成后发布。若检查过程中代码继续变化，脚本会停止。发布不会同步业务图片、账号、密钥或本机缓存。若更新本机执行器相关代码，等作业空闲后重启执行器。

## 服务器维护位置

| 位置 | 用途 |
| --- | --- |
| `/opt/image-workbench/current` | 当前程序版本 |
| `/opt/image-workbench/releases/` | 保留的发布版本 |
| `/var/lib/image-workbench` | 独立业务数据，归属 `image-workbench` 服务用户 |
| `/etc/image-workbench.env` | 服务配置与执行器令牌，仅 root 可读 |
| `image-workbench.service` | 网站服务，绑定 `127.0.0.1:7860`，已启用开机启动 |
| `/etc/nginx/sites-available/zz-image-workbench` | IP 的 HTTP 跳转、HTTPS 反向代理与证书验证入口 |
| `/etc/image-workbench-acme/` | 图片网站专用 ACME 配置及 IP 证书 |
| `image-workbench-cert-renew.timer` | 每天检查两次证书续期，成功后重新加载 Nginx |

IP 证书已签发且公网 TLS 校验正常；自动续期已完成模拟验证。证书使用 Let's Encrypt 的短期 IP 证书机制，详情见 [官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html)。续期使用独立 Certbot 环境，原有域名网站的证书配置未替换。部署后已确认原有域名网站仍返回 HTTP 200。

云端发布包只包含运行程序。管理员后台的本地测试、打包和发布工具不随包上传；普通工作台与任务链路正常，维护部署从本机执行。

## 验收证据

公网登录页及静态资源返回 HTTP 200，未登录业务接口与未认证 worker 接口返回 HTTP 401。真实生图链路验收已通过：云端测试账号提交一张图，本机 Codex 生成、回传，云端下载及审核完成；本机产物与云端下载文件的 SHA-256 一致，执行器检查点为 `done`。测试账号、测试图片及验证临时副本保留供核对。

验收报告为 `reports/tencent_live_acceptance.json`（`passed=true`），图片为 `reports/tencent-live-result.png`。这次实际调用了一次图片生成，测试账号使用 40 积分。当前真实验收覆盖生成链路；参考图和编辑链路此前已在独立 HTTP 演练与回归测试中验证，本次没有额外消耗生图额度重跑。

收尾的临时文件清理命令被自动审批拒绝，仅返回 `blocked by policy`；没有执行该命令中的文件删除或测试账号禁用。保留的短路径验证目录为 `D:\iwcheck-4b4191bf`，不参与线上服务运行。管理员和执行器凭据仍在受保护的本机与服务器目录中。
