# 腾讯云收需求，本机执行生图

新增两种启动方式：`cloud` 运行云端网站和持久队列，`remote-worker` 在你的电脑上领取需求、下载参考图、调用现有 Codex、回传图片。原来的 `web` 和 `worker` 继续用于单机运行。

```text
浏览器 → 腾讯云网站 / 持久队列
                 ↑       ↓
            本机主动 HTTPS 连接
                 ↓       ↑
           本机执行器 → 本机 Codex
```

服务器不需要安装或登录 Codex。本机沿用自己的 Codex 登录、网络环境和生图额度；云端只保存业务需求、参考图、任务状态和回传结果。本轮验证使用演练图片，没有调用真实生图。

## 已具备的行为

- 本机离线时，服务器接收的新需求保持排队。本机上线后领取。
- 参考图和历史编辑基底都按内容哈希校验，修改结果保留原图关联。
- 领取和启动许可落盘。同一任务只允许同一次本机执行；另一个执行器不能抢走已领取任务。
- 图片先保存在本机持久缓存。回传中断、回执丢失或服务重启后，只重传原图片；云端归档使用固定图片 ID。
- 本机生成进程状态无法确认时，网站显示“状态待确认”，保留该任务的执行名额和积分冻结，不自动重新生图；默认并发为 1 时会阻止后续任务领取。
- 网站采用个人账号与 Workspace 登录隔离，执行器使用独立 Bearer 令牌。项目、任务、图片和积分都受账号认证保护；登录页及静态资源可公开访问，浏览器不能使用执行器接口。

当前支持多账号、Workspace 和积分控制。远程模式支持配置多个执行名额，默认全站 1、每网站账号 1；每个执行器仍顺序处理任务。电脑关机、睡眠或执行器退出期间不能由该电脑生图。云端按执行器单独检查心跳，失去心跳 60 秒后对应活动任务显示“状态待确认”；已领取任务不会因超时转给另一台电脑。

## 执行并发配置

在云端环境配置中设置下面两项，重启云端服务后生效；管理员页面的“运行配置 → 执行并发”显示当前生效值和名额占用。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `IMAGE_WORKBENCH_MAX_ACTIVE_TOTAL` | `1` | 全站已领取与结果待确认任务的总上限，整数 `1..32` |
| `IMAGE_WORKBENCH_MAX_ACTIVE_PER_ACCOUNT` | `1` | 同一网站账号占用的执行名额上限，整数 `1..32` |

两项均不接受 `0` 表示无限。`MAX_PENDING_*` 控制提交时的待处理容量，`MAX_ACTIVE_*` 控制领取时的执行名额；排队、执行中与结果待确认的任务都可能已经冻结积分，不能把冻结任务数当作正在生图数。

提高远程全站上限后，需要多个执行器进程及各自独立、持久的缓存目录才能并行；共用一个缓存仍只允许一个进程执行。这里的网站账号不等于上游 ChatGPT 账号，配置不会新增上游额度，也没有建立上游账号池。本地 `web` / `worker` 模式仍为串行，即使配置大于 1，其有效并发上限也为 1。

领取在同一控制锁内检查总量、账号占用并持久化归属。若队首账号已占满，会选择后续有名额的账号；旧任务缺失账号信息时共享一个保守的账号名额。同一执行器重领先返回原作业；下调上限不会丢弃已领取任务，新任务等占用降到新上限后才领取。心跳过期、断线或 `uncertain` 不释放名额，仍按本文的中断处理流程确认。剩余名额只表示调度允许量，不代表在线执行器数量。

## 服务器配置

服务器需要 Python 3.10+ 及 venv（本次本机实测 Python 3.12）。版本保存在 `/opt/image-workbench/releases/版本号/`，`/opt/image-workbench/current` 指向当前版本，业务数据放在 `/var/lib/image-workbench`。使用专用普通用户 `image-workbench`，给它数据目录读写权限。新的一键发布脚本会为每个版本创建虚拟环境并安装 `requirements-web.txt`，操作见 [一键同步部署](one_click_deploy.md)。

将 `deploy/cloud.env.example` 的真实配置放到 `/etc/image-workbench.env`，限制为服务管理员可读。两个配置项：

| 配置 | 用途 |
| --- | --- |
| `IMAGE_WORKBENCH_PUBLIC_ORIGIN` | 外部完整源站，例如 `https://images.example.com`，不能带路径 |
| `IMAGE_WORKBENCH_WORKER_TOKEN` | 云端、本机相同的随机执行器令牌，至少32个 ASCII 字符 |

执行器令牌可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成，示例占位值会被拒绝。旧 `IMAGE_WORKBENCH_WEB_USER` / `IMAGE_WORKBENCH_WEB_PASSWORD` 不再用于浏览器登录，用户在登录页设置和输入个人密码。已有数据先按 [账号迁移说明](accounts_workspaces_credits.md) 预览、备份并迁移；新空目录会预留 `moy` 管理员。

`deploy/image-workbench.service` 是 systemd 服务模板。安装该文件并完成目录、用户和环境配置后启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable image-workbench
```

首次发布创建 `current` 版本链接后，发布脚本再启动服务。服务仅监听服务器的 `127.0.0.1:7860`。使用 `deploy/nginx.conf.example` 配置 HTTPS 反向代理，替换域名和证书路径。代理需保留 Host、Authorization，请求大小上限至少41 MiB；外部源站必须与配置一致。

对公网使用 HTTPS。当前未提供服务器 IP、账号或域名，systemd、Nginx 模板尚未在真实腾讯云实例安装或验证。

## 本机配置

在这台已经有 Codex 登录的电脑安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
```

复制 `deploy/remote-worker.example.json` 到个人配置目录，例如 `C:\Users\win\.image-workbench\remote-worker.json`，填写服务器 HTTPS 地址、与服务器相同的执行器令牌，以及一个独立、持久的缓存目录。真实配置包含凭据，不提交到仓库。

然后在项目目录运行：

```powershell
.\scripts\start_remote_worker.ps1 -ConfigPath "C:\Users\win\.image-workbench\remote-worker.json"
```

保持进程运行。它每5秒检查新任务，执行中每15秒发送心跳。用户在网页点击开始生成才会执行；脚本启动本身不创建生图任务。`-Once` 只处理至多一项；日常保持默认常驻。脚本不会把令牌放进命令参数或打印出来。

缓存中包含执行器身份、每项任务的检查点、参考图、原始结果和日志。不要把它放在临时目录，不要在有未回传任务时清空或更换。更换服务器或演练/真实后端时必须使用独立缓存。

如果暂时没有域名，可先通过 SSH 本地转发做私有联调：

```powershell
ssh -N -i "C:\Users\win\.ssh\tencent_image_site_rsa" -L 17860:127.0.0.1:7860 用户名@服务器公网IP
```

这时服务器 `IMAGE_WORKBENCH_PUBLIC_ORIGIN` 和本机配置 `server` 都设为 `http://127.0.0.1:17860`，浏览器也访问该地址。HTTP 仅允许本机地址；公网地址必须使用 HTTPS。回环请求不会经过系统代理，Codex 原有代理配置保持不变。

## 中断后的处理

网络中断：保持或重新启动执行器，原缓存会自动续传。已完成的任务不重新生成；上传失败不需要点击网页“重试”。

服务器归档失败：如果网站显示可恢复归档，点击“恢复归档结果”，导入原候选图片，不调用 Codex。

本机执行中退出：再次启动时会报告 `uncertain`。查看缓存对应任务目录里的 `process.json`、`stdout.log`、`stderr.log`，确认原 Codex 子进程已经停止。确认后运行：

```powershell
.\scripts\start_remote_worker.ps1 -ConfigPath "C:\Users\win\.image-workbench\remote-worker.json" -ConfirmStopped 作业ID -Once
```

这个命令只将任务明确标记为中断；需要再次生图时由用户在网站显式重试。不要在原进程仍可能执行时解除。

缓存丢失：优先恢复原缓存。服务器的启动许可会拒绝用新检查点重复执行旧作业。若确认旧执行器及其生图子进程均已停止且不需要恢复原结果，可在服务器使用原有 `worker-confirm-stopped --project 存储项目ID --task 任务ID --confirm-stopped` 命令解除该作业，再由网页显式重试。未完成结果可能仍保留在本机 Codex 缓存，需要人工核查。

不自动迁移原 `data/`，也不在生图进行时迁移。首次部署先使用独立服务器数据目录；需要搬旧项目时先停止原 Web/Worker、确认活动任务，备份整个数据根目录后再迁移。

## 验证与证据

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 scripts/remote_rehearsal.py
```

自动测试覆盖登录隔离、参考图与编辑基底、领取/启动/上传/完成回执丢失、服务器重启、生成中断、缓存缺失防重跑、归档崩溃恢复和图片完整性。`remote_rehearsal.py` 临时启动两个独立 CLI 进程，经真实回环 HTTP 执行网页提交、领取、回传、审核、下载和历史图编辑；结束后停止自己的进程并清理自己的临时数据。

结果保存到 `reports/remote_regression_tests.txt` 与 `reports/remote_http_rehearsal.json`。真实腾讯云网络、HTTPS 配置、本机现有 Codex 的一次真实生图与编辑需在获得服务器连接信息后联调。
