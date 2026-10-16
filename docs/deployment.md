# 部署与维护

网站和执行器可以在同一台机器，也可以分离。网站保存账号、项目、任务、积分账本和结果；图片与 H3 执行器在拥有相应模型或服务登录环境的机器上运行。部署包不携带这些登录环境。

## 单机

按 [README](../README.md) 安装依赖，运行：

```text
python -m image_workbench --data-root data web --port 7860
```

这个命令绑定 `127.0.0.1` 并启动本机图片 worker。另开一个终端运行 H3 worker，两者的 `--data-root` 必须指向同一目录。H3 配置放在仓库根目录时可使用示例中的 `workflows/minimax_h3` 相对路径。

## Linux 云端接收任务

先为云端创建独立 Python 虚拟环境并安装 `requirements-web.txt`，准备受服务账号管理的数据目录。使用本机生成的随机值替换执行器令牌占位符；不要使用示例值。

```sh
export IMAGE_WORKBENCH_PUBLIC_ORIGIN=https://images.example.com
export IMAGE_WORKBENCH_WORKER_TOKEN='REPLACE_WITH_RANDOM_WORKER_TOKEN_AT_LEAST_32_ASCII_CHARACTERS'
export IMAGE_WORKBENCH_REGISTRATION_MODE=invite
python -m image_workbench --data-root /var/lib/image-workbench cloud --port 7860
```

`cloud` 只绑定回环地址；通过你自己的 HTTPS 反向代理发布页面。[Nginx 示例](../deploy/nginx.conf.example) 包含所需 Host/Authorization 转发与上传大小设置，域名和证书路径需自行替换。

首次启动后，在服务器上取得 `/var/lib/image-workbench/.identity/moy-activation.json` 中的启用令牌，在网页登录页启用 `moy` 管理员。该文件和账号数据库不应出现在公开仓库、镜像层或工单中。

使用 `invite` 模式时，可在受信任服务器终端为对应数据目录创建邀请码：

```text
python -m image_workbench --data-root /var/lib/image-workbench accounts-invite
```

注册模式可以是 `open`、`invite` 或 `closed`。部署示例还提供账号总量、排队数量、日积分提交预算和图片执行并发上限。它们是服务运营限制，不能代表显卡性能或上游账号额度。H3 执行器仍使用独立的串行队列与 GPU 锁。

视频默认只开放 720p。验证实际硬件可以完成 1080p 后，在网站的环境配置中设置 `IMAGE_WORKBENCH_ENABLE_1080P=1` 并重启网站进程，才会开放 1080p。删除该变量或设为 `0` 会关闭新提交入口；已有执行任务与历史作品保持可恢复、可下载。执行器不需要这个开关。

## 本机连接云端

图片执行器可以把 [remote-worker.example.json](../deploy/remote-worker.example.json) 复制为 `deploy/remote-worker.local.json`，填写网站源站、同一个执行器令牌和独立持久缓存目录：

```powershell
powershell -NoProfile -File scripts/start_remote_worker.ps1 -ConfigPath deploy/remote-worker.local.json
```

H3 执行器使用仓库根目录的 `h3-worker.local.json`。在已有本机配置中加入 `"server": "https://images.example.com"`，通过环境变量 `IMAGE_WORKBENCH_WORKER_TOKEN` 提供令牌，或复用本机私有 worker 配置：

```powershell
powershell -NoProfile -File scripts/start_h3_worker.ps1 -ConfigPath h3-worker.local.json -WorkerConfigPath deploy/remote-worker.local.json
```

每个 worker 的缓存目录必须持久保留。图片缓存、H3 缓存、网站数据目录应分别使用不同位置。H3 的相对配置路径按配置文件目录解析；让访问同一 ComfyUI/GPU 的 H3 worker 共用相同的 `gpu_lock_path`。

ComfyUI 地址只允许本机回环 HTTP 服务，正常部署不需要开放 ComfyUI 端口到公网。云端网站地址使用 HTTPS；HTTP 仅用于本机或 SSH 隧道联调。

## systemd 与代码更新

[systemd 示例](../deploy/image-workbench.service) 使用以下通用布局：

```text
/opt/image-workbench/releases/<release-id>/   每个版本独立代码和虚拟环境
/opt/image-workbench/current                 指向当前版本的符号链接
/var/lib/image-workbench/                    独立业务数据目录
/etc/image-workbench.env                     服务器私有环境配置
```

首次部署需要管理员准备 `image-workbench` 服务账号、目录权限、私有环境文件、初始版本与 `current` 链接，再安装服务文件。`scripts/apply_release.sh` 是后续版本更新器；它会校验现有布局，不负责从空服务器安装全部基础设施。

在开发机器先验证并打包：

```text
python scripts/deploy_release.py --package-only
```

需要实际更新服务器时，将 `deploy/server.example.json` 复制为 `deploy/server.json`，填写自己的 SSH 主机、用户、端口和私钥路径，确认 SSH 主机指纹已加入本机 `known_hosts`。然后运行：

```text
python scripts/deploy_release.py --config deploy/server.json
```

脚本会运行回归、校验包摘要、上传代码、建立新版本、切换 `current` 并验证健康状态；新版本失败时尝试回退代码。它不会同步业务数据、服务器环境文件或执行器缓存。非 root SSH 用户需要已有的非交互 sudo 权限。更新执行器代码应在相关任务空闲时单独进行。

## 备份与恢复

备份前先停止网站和所有会写入同一数据目录的执行器；检查没有仍在写入的进程，再使用完整数据目录备份。图片、视频、账号和积分必须一起保留。

```text
python scripts/backup_workbench.py backup --data-root data --destination C:/path/to/new-backup --writers-stopped
python scripts/backup_workbench.py verify --backup C:/path/to/new-backup
python scripts/backup_workbench.py restore --backup C:/path/to/new-backup --destination C:/path/to/new-restored-data
```

目标必须是新的隔离目录。恢复不会自动启动服务，也不会覆盖正在使用的数据目录。另行保存服务器环境配置与执行器持久缓存；它们不一定在网站数据根目录内。恢复后先核对任务与积分状态，再切换服务目录。

## 实测范围

仓库测试对任务恢复、权限、账本和文件边界使用模拟上游与合成素材。实际图片生成依赖当前 Codex CLI 与账号能力；实际 H3 生成依赖模板节点、权重文件、显存和 FFmpeg。首次部署应使用自己的小样本测试所需模式及分辨率，确认输出尺寸、时长与下载结果。

执行器连接状态只表示近期收到 worker 活动，不是生成成功承诺。`uncertain` 状态表示可能已提交但结果未能确认：它保留积分与执行占位，不自动重生。处理时应先检查原任务与缓存，而不是删除检查点后重新启动。
