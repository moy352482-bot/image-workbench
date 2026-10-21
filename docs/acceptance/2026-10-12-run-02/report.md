# 第二阶段真实链路验收通过（2026-10-12）

已实际完成：创建生成任务 → 本地 Codex 生图 → 校验并自动归档 → 查询图片路径 → 基于历史图片创建编辑任务 → Codex 编辑 → 归档新版本。此次使用现有 ChatGPT 登录，没有使用额外图片 API。

## 超时原因与修复

Windows 已启用现有系统代理，但独立 Codex CLI 的 `respect_system_proxy` 默认为 false。自带 doctor 检查显示 HTTP 可达，而 Responses WebSocket 握手在 15 秒内超时；前一轮真实运行多次重连后才回退到 HTTPS。

只对此次 doctor 启用 `--enable respect_system_proxy` 后，握手返回 `HTTP 101 Switching Protocols`，耗时 1234 ms。证据见 [proxy-doctor.json](proxy-doctor.json)。

因此在 `image_workbench/codex_backend.py` 的命令参数中加入同一开关。仅影响工作台启动的子进程，使用用户已经配置的系统代理，不写入全局 Codex 配置，不改变登录方式或 provider。`tests/test_codex_backend.py` 同步覆盖该参数及原有认证、只读执行和日志行为。

本机 CLI 把此开关标记为 under development，会输出提示；提示保留在日志中。实际握手、生图、编辑及归档均已验证成功。

## 真实验收结果

项目：`b8a59704d4ec46f4ae726c8cc3aea315`。

| 项目 | 原图生成 | 基于原图编辑 |
| --- | --- | --- |
| 需求 | 仙侠风格成年女修人物图，青白衣袍、佩剑、全身展示 | 青绿色服饰细节改浅紫，其余保持 |
| 任务 ID | `326e8358d38345b1ad905c92766bcbc1` | `ac316f526b7b476bb4eff6959f7ab359` |
| 执行 ID | `b471b81ca22b420888c33cfe2a862dde` | `976e423a74ae424f8883a64b9c2bc6cb` |
| 图片 ID | `c667e44b056340e0b13a119019d95d24` | `7e7fee5b9651402eb8aafc0c4827668c` |
| 结果 | succeeded | succeeded |
| 总耗时 | 44.163 秒 | 61.951 秒 |
| 规格 | PNG，1024 × 1536，2,548,101 bytes | PNG，1024 × 1536，2,348,289 bytes |

原图于北京时间 12:14:26 完成登记；编辑版于 12:16:58 完成登记。

已分别打开图片进行视觉检查：原图符合仙侠女修主题；编辑版将服饰和飘带改为浅紫，保留了同一人物、姿势及背景。额外用 Pillow 完整解码，并核对尺寸、文件哈希和登记信息。

- 原图：[查看 PNG](../../../data/projects/b8a59704d4ec46f4ae726c8cc3aea315/tasks/326e8358d38345b1ad905c92766bcbc1/outputs/c667e44b056340e0b13a119019d95d24.png)
- 编辑版：[查看 PNG](../../../data/projects/b8a59704d4ec46f4ae726c8cc3aea315/tasks/ac316f526b7b476bb4eff6959f7ab359/outputs/7e7fee5b9651402eb8aafc0c4827668c.png)
- [机器可读验收证据](result.json)：包含完整提示词、原始缓存路径、归档路径、哈希、执行时间及参考图参数。

编辑版的 `source_artifact_id` 正确指向原图。真实命令通过 `--image` 传入本次执行目录中的基底图快照，快照哈希与原图一致。编辑完成后再次读取原图，其哈希仍为 `fe798225f86d6e6f641ff3f2b0acde8d0cf2c3e31ca63640efc6a71aede9d585`，没有覆盖旧版本。

## 自动化回归

运行 `python -X utf8 -m unittest discover -s tests -v`：29 项，28 项通过，1 项跳过，0 失败，用时 4.865 秒。

跳过项是 Windows 文件符号链接创建（WinError 1314）；目录 junction 检查通过。自动化测试本身不调用图片模型，真实验收是分开的显式执行。

## 失败记录与授权范围

第一轮 300 秒超时的日志保留在 [前次验收记录](../2026-10-12-run-01/report.md) 中。用户随后明确授权自主使用生图额度测试和修复。

第二次执行 `d5a62a241db94481a81e534403b76e48` 在重连期间查明代理问题后主动终止，未返回产物。终止前核对了 PID、可执行文件和执行 ID；原因保存在该执行目录的 `work/manual-stop.json`。第三次原图生成及后续编辑是修复后的两次成功执行。

执行器仍不在超时后自动重试。此次重试由已获得用户授权的开发验收流程显式发起。前两次未成功返回的请求，其远端生成或额度状态无法从本地日志确定；不能将“没有本地图片”解释为必然没有额度消耗。

## 复查命令

```powershell
python -m image_workbench status --project b8a59704d4ec46f4ae726c8cc3aea315 --task 326e8358d38345b1ad905c92766bcbc1
python -m image_workbench outputs --project b8a59704d4ec46f4ae726c8cc3aea315 --task 326e8358d38345b1ad905c92766bcbc1
python -m image_workbench outputs --project b8a59704d4ec46f4ae726c8cc3aea315 --task ac316f526b7b476bb4eff6959f7ab359
```

当前仍为可信本地、单进程顺序执行。此次没有开发网页，也没有新增多 Agent 运行系统。
