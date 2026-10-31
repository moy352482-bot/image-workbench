# 一键同步部署

也可以从本机工作台的[管理中心](admin_console.md)准备更新包，查看功能测试报告，再预览并确认部署。界面复用本文的发布流程。

首次完成腾讯云服务器初始化并填好连接配置后，每次改完网站，在本机项目目录执行：

```powershell
cd "D:\ChatGPT\图片网站"
.\scripts\deploy.ps1
```

命令会运行本地回归检查 → 打包应用程序 → 使用 SSH 上传 → 在服务器独立目录安装依赖 → 切换版本并重启服务 → 检查登录入口。成功后刷新网页即可使用新版。

腾讯云服务器已完成首次配置，当前入口为 [https://124.156.182.151](https://124.156.182.151)。本机 `deploy/server.json` 已设置服务器连接，后续保存代码后可直接运行上面的发布命令。账号启用、本机执行器和维护位置见 [当前部署说明](tencent_live_deployment.md)。

## 只做一次的本机配置

在项目目录复制示例：

```powershell
Copy-Item -LiteralPath .\deploy\server.example.json -Destination .\deploy\server.json
```

用编辑器打开 `deploy/server.json`，填写：

```json
{
  "host": "服务器公网IP",
  "user": "ubuntu",
  "port": 22,
  "private_key": "C:\\Users\\win\\.ssh\\tencent_image_site_rsa"
}
```

`user` 按实际系统填写，例如 `ubuntu` 或 `root`。`private_key` 指向本机私钥文件，不填写 `.pub` 公钥，也不把私钥内容粘贴进配置。此连接配置已加入 Git 忽略。

首次使用需通过普通 SSH 登录，并核对服务器主机指纹。后续脚本启用严格主机校验，不会自动信任新主机或忽略主机变化。脚本采用非交互登录；加密私钥需先加入已配置的 SSH agent。非 root 账号需已具备执行发布脚本所需的 `sudo -n` 权限。

## 服务器首次准备

发布脚本面向 Linux/systemd，使用以下固定位置：

| 位置 | 用途 |
| --- | --- |
| `/opt/image-workbench/releases/版本号/` | 每次发布的独立程序与虚拟环境 |
| `/opt/image-workbench/current` | 当前运行版本的符号链接 |
| `/var/lib/image-workbench` | 持久业务数据 |
| `/etc/image-workbench.env` | 服务器访问地址及执行器令牌等配置 |
| `image-workbench.service` | systemd 服务 |

首次需要准备 Python 3.10+ 及 venv、专用服务用户 `image-workbench`、上述目录与环境配置，并安装本项目 `deploy/image-workbench.service` 模板。模板的 WorkingDirectory 和 ExecStart 均使用 `current`。初次只需启用服务；第一次发布创建 `current` 后才启动。HTTPS 反向代理继续按 [远程部署说明](remote_deployment.md) 配置。

如果服务器已经按旧说明直接运行 `/opt/image-workbench/.venv`，必须先完成目录布局和服务配置切换，不能直接覆盖。更新脚本会检查并拒绝不匹配的服务配置。

## 日常发布

1. 在本地改好并保存网站功能。
2. 运行 `.\scripts\deploy.ps1`，等待“服务器版本更新成功”。
3. 刷新网站。若本次也更新了本机执行器代码，等当前任务结束后重启本机执行器。

如果发布检查期间又修改了程序文件，脚本会停止；保存完成后重新运行。检查失败或依赖安装失败时，不切换服务器版本。版本切换需要重启网站，会有短暂访问中断；本机已经生成的图片保留在缓存中，断线后按原协议回传。

启动检查失败会尝试恢复上一版程序并检查旧版状态。旧发布目录保留，不自动删除。回退只涉及代码，不能撤销数据库或数据格式变更；涉及数据迁移的版本需另行备份、验证迁移和回退方法，发布脚本不会主动执行迁移命令。

如果 SSH 在远程执行期间中断，不能仅根据本机退出结果判断线上版本，需先检查服务器 `current` 指向和 `systemctl status image-workbench`。

## 尚无服务器时先演练

```powershell
.\scripts\deploy.ps1 -PackageOnly
```

这会实际运行本地检查并生成 `.deploy/版本号/release.tar.gz`，不连接服务器。该目录还保存检查输出 `checks.txt` 和文件校验清单 `manifest.json`。

发布包包含 `image_workbench/` 程序与网页资源、两个运行依赖清单、版本清单，以及脚本、分镜、H3 的运行配置和对应 Skill、模板、案例及许可来源文件。`skills/image-generation-default/` 的默认图片规则、风格目录和来源说明也随包上传，8 张风格预览图位于网页静态资源目录。不会上传其他配置、本机 `data/`、账号登录缓存、SSH 私钥、`.venv/`、本机任务缓存、报告或服务器配置文件。服务器在切换版本前检查脚本模板、分镜规则、模型配置和默认图片 Skill 可加载。
提示词强化还随包发布 `config/prompt_enhancement.json`、提示词 Skill 与图片风格 JSON，切换前检查可加载。

部署脚本的打包边界、输入校验、检查失败拦截和代码变动拦截已经自动验证；健康检查失败回退在 WSL/Linux 中使用模拟 systemd 验证。腾讯云首次发布已验证真实 systemd、依赖安装、当前版本切换、登录入口和公网 HTTPS。真实服务回退尚未人为制造故障演练。
