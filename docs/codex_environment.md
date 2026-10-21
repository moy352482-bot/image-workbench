# 本机 Codex 环境检查（2026-10-12）

**最新结论：真实非交互图片生成及基于历史图片编辑均已成功，并完成自动归档。** 首轮超时来自独立 CLI 没有沿用 Windows 的现有系统代理；启用本次子进程的 respect_system_proxy 后，WebSocket 握手约 1.2 秒成功，真实生成约 44 秒、编辑约 62 秒。详见 [最新验收记录](acceptance/2026-10-12-run-02/report.md)。以下保留初次检查和排错过程。

- 第一阶段回归：11 项测试全部通过，无失败、无跳过。
- Python 3.12.10，Pillow 11.3.0。
- codex-cli 0.162.0-alpha.17.2；可从 PATH 找到实际 codex.exe。
- `codex login status` 返回 Logged in using ChatGPT；未读取或输出登录令牌。
- `codex exec --help` 确认非交互、--image、--json、--output-schema、--output-last-message、read-only sandbox、--ephemeral、--ignore-user-config 可用。
- `codex features list` 报告 image_generation 为 stable/true。这是功能开关证据，不等于实测图片工具可用。
- 本机 ImageGen 技能位于 `C:/Users/win/.codex/skills/.system/imagegen/SKILL.md`。
  技能说明内置工具默认保存到 CODEX_HOME 下，项目产物应从 generated_images 复制出来；不应假定工具支持任意输出目录参数。
- 在获准的执行环境中尝试一次纯文本非交互探针，显式禁用 image_generation、shell 和其他工具，45 秒内未完成并终止。因此非交互完整请求链路尚未验证成功，不能把此结果解释为图片能力必然不可用。
- 初次环境检查及自动化测试没有发起真实图片调用；后来获授权的真实验收另行记录。
- 第二阶段最终回归：共 29 项，28 项通过、1 项跳过、0 失败。跳过项为缓存文件符号链接验证（WinError 1314）；存储层目录 junction 验证通过。

安全复制的代码路径已用合成的 Codex 缓存目录和本次结构化回复测试；后续真实验收也已确认缓存产物被安全复制、校验并登记到任务目录。
如果真实运行报告内置图片工具 unavailable，后端会失败并保留日志，不使用收费 API、网页逆向或浏览器 Cookie 替代。

补充：用户随后授权一次真实生图验收，要求仙侠女修人物示意图。该请求经历 WebSocket 重连和 HTTPS 回退，在 300 秒后超时；没有返回或登记图片。执行器保存了 `timed_out` 状态、输入和日志，已确认子进程退出，未自动再次生图。不能确认远端是否计入生成额度。详细证据见 [验收记录](acceptance/2026-10-12-run-01/report.md)。

官方参考：[非交互执行与结构化输出](https://learn.chatgpt.com/docs/non-interactive-mode)、
[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。本机 CLI help 与已安装技能用于确认本机版本的具体能力。
