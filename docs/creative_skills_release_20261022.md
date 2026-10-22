# 创作 Skill 线上发布记录

2026-10-22，按用户部署指令发布至 https://124.156.182.151。

- 发布版本：`20261022T070208Z-4635fa2d`；上一版：`20261020T110008Z-e3ef56a0`，旧程序目录保留。
- 源码：本工作区 `codex/01a158360eab7c11a59969f5c16906c3`，提交 `e6fd41a`。包含创作 Skill，并合入汇总分支 `9ee18be` 的公共端口、阶段协调、恢复诊断和页面异步状态修复。未修改汇总工作区。
- 完整检查由发布入口调用 `scripts/verify.py`：701 项，697 通过，4 项跳过（Windows 符号链接 2 项、Linux Bash 模拟 2 项）；Ruff 无新增诊断，架构检查 85 模块、421 导入、0 违规。发布前另在本机实际执行了远端解包校验代码，确认完整包可接收且额外文件被拒绝。
- 服务器服务 active/running，进程工作目录为本次 release；179 个实际运行文件与本机清单逐项匹配。
- 在服务器真实安装目录中编译 script、screenplay、storyboard 三类输入，确认专项方法正确分流，分镜镜头设计与连续性规则完整进入输入。只编译输入，没有调用模型或创建测试业务任务。
- 公网 `/login`、`/api/auth/config` 和 video/pipeline/storyboard 静态脚本均为 200；静态脚本摘要匹配发布包。未认证的脚本、分镜配置接口为 401。
- 脚本和分镜价格配置与发布前完全一致；真实分镜仍未定价且关闭，Mock 开放。启用真实分镜属于另一个需要明确模型、执行通路和定价的操作。
- 本机脚本执行器原先离线，缓存检查点均为 done。恢复后服务端确认 1 个新鲜心跳，模型为 `gpt-6-astra`、`gpt-6.1-sol`、`gpt-6-luna`，本机状态 idle。它使用本工作区代码，进程及日志记录在 `%LOCALAPPDATA%/ImageWorkbench/Tencent/script-skill-worker-process.json`。

发布只切换程序版本，业务数据、凭据和价格未替换。本次没有真实模型生成或测试积分消费。

证据位于当前工作区：

- [完整检查](../.deploy/20261022T070208Z-4635fa2d/checks.txt)
- [发布清单](../.deploy/20261022T070208Z-4635fa2d/manifest.json)
- [部署日志](../work/creative-skills-release/deploy.log)
- [线上文件、服务与 Skill 核验](../work/creative-skills-release/live-verification.json)
- [执行器在线状态](../work/creative-skills-release/worker-online.json)

方法拆解与兼容说明见 [两套 Agent 内置创作方法](creative_agent_skills.md)。
