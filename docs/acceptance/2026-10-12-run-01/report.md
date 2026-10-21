# 第二阶段验收记录（2026-10-12）

这是首轮失败的历史记录。用户后续授权自主排错和重试，真实生成与编辑已通过，见 [最新验收记录](../2026-10-12-run-02/report.md)。

结论：自动化测试与本地后端完整流程通过；真实 Codex ImageGen 请求超时，尚未获得或登记图片，不能判定真实生图链路通过。

## 本地验收

- `python -X utf8 -m unittest discover -s tests -v`：29 项，28 项通过、1 项跳过、0 失败，用时 4.807 秒。
- 跳过项：Windows 无文件符号链接创建权限（WinError 1314）。目录 junction 检查通过。
- 额外启动 8 次独立 CLI 进程，验证创建项目、带参考图创建任务、本地执行、查询输出、引用旧图片创建编辑任务、再次执行和状态查询。全部通过，旧图内容和路径保留，版本来源正确。
- 自动化测试及本地流程均未调用图片模型。本地流程的临时图片和数据已清理。
- 证据：[自动化与环境检查](automated-checks.json)、[本地流程](local-flow.json)。

## 用户授权的一次真实请求

用户明确选择“包含一次真实生图”，随后指定“仙侠风格的女修人物示意图”。实际使用的完整提示词及任务记录见 [prepared.json](prepared.json)。

- 项目 ID：`b8a59704d4ec46f4ae726c8cc3aea315`
- 任务 ID：`326e8358d38345b1ad905c92766bcbc1`
- 执行 ID：`a28b22d6775045e786ca582c5c4bfaf1`
- 命令：`python -X utf8 -m image_workbench run --project b8a59704d4ec46f4ae726c8cc3aea315 --task 326e8358d38345b1ad905c92766bcbc1 --backend codex --allow-image-generation --timeout 300`
- 开始：2026-10-12 11:19:34（北京时间）；结束：11:24:34。
- 结果：CLI 退出码 1；任务状态 `timed_out`；候选文件清单和正式输出均为空。
- Python 执行器仅启动一次真实 Codex 进程。已确认其 PID 7164 在超时后退出；未重新发起生图。

日志显示 WebSocket 连接多次超时，随后 Codex 自行切换到 HTTPS，并回复正在生成。但在总计 300 秒内没有返回产物路径，stderr 另有可用模型列表刷新超时。日志没有完整的图片工具结果，不能据此确认远端生成是否已开始、是否取消或是否计入额度。

提示词、状态和原始日志均保存在项目内：

```text
data/projects/b8a59704d4ec46f4ae726c8cc3aea315/tasks/326e8358d38345b1ad905c92766bcbc1/
  logs/runs/a28b22d6775045e786ca582c5c4bfaf1/
    execution.json
    stdout.log
    stderr.log
    work/request.json
    work/command.json
    work/process.json
```

超时状态保存、日志保留、错误退出码以及避免登记不存在图片均已在真实失败路径中验证。真实图片的格式检查、自动复制归档、预览及基于该图片创建编辑任务尚未验证；这些行为目前只有本地测试证据。

## 额外连接诊断

尝试了明确禁用 `image_generation` 的纯文本探针，测试仅对此次子进程关闭 WebSocket。该尝试在配置解析阶段退出，没有发送推理或图片请求。

官方[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)列有自定义 provider 的 `supports_websockets` 字段，但本机 CLI 拒绝覆盖内置 `openai` provider：`Built-in providers cannot be overridden`。因此没有把未经验证的参数加入执行器，也没有修改全局配置、provider、登录方式或使用其他 API。诊断文件保存在 [http-probe](http-probe/) 中。

下一次真实尝试应由用户再次选择启动，可将超时延长到 600 秒，为连接回退和图片生成留出更多时间。延长超时并不保证成功；当前阻碍仍是本机 CLI 请求未在期限内返回可验证产物。
