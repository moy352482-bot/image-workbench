# 架构治理验收记录

日期：2026-10-20；执行环境：本机 Windows / Python 3.12，临时数据和本地/Mock 后端。
结论：架构审计、七个上游项目研究、低风险防护、脚本插件试点和开发规范已完成。
保留模块化单体及既有任务/积分/存储协议，未实施全面重构。

## 验证结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 修改前完整验证 | 639 项：635 通过、4 跳过 | `work/architecture-governance-20261020/baseline-verify.log` |
| 最终统一验证 `scripts/verify.py` | 652 项：648 通过、4 跳过，退出码 0 | `work/architecture-governance-20261020/final-verify.log` |
| 其中浏览器行为测试 | 35 项全部通过 | 同上；既有测试另生成 `docs/goals/asset-ux/` 报告与截图 |
| 插件 + Ruff 基线算法专项 | 12 项全部通过；另有 1 项架构负例纳入完整验证 | `work/architecture-governance-20261020/plugin-tests.log` |
| Ruff 0.17.1 | 基线 134 条，当前 132 条，新增 0；没有全库自动修复 | `tests/contracts/ruff-baseline.json`、最终验证日志 |
| 架构检查 | 81 个模块、406 条导入记录、0 违规/静态循环 | `scripts/check_architecture.py --json`；最终验证日志 |
| 文档 | 新架构文档与 AGENTS 本地链接均可解析，源码引用经复核 | `docs/architecture/` |

4 项跳过与修改前一致：两项 Windows 文件符号链接权限限制、两项 Linux Bash 发布模拟。
本轮没有在 WSL 重新运行发布模拟，因此不声称这些检查已通过。
验证日志中的 Starlette/httpx 弃用提示是既有依赖提示，不是本次回归失败；未顺手升级 Web 依赖。

完整验证前后源码指纹一致。后续仅补充文档、计划与证据记录，没有继续修改被测业务代码。

## 需求与交付对应

| 用户验收点 | 交付 |
| --- | --- |
| 实际职责、核心边界和 TaskRunner 评估 | [CURRENT_ARCHITECTURE](CURRENT_ARCHITECTURE.md)、[MODULE_MAP](MODULE_MAP.md) |
| 循环、跨层、高 fan-in、重复和回归风险 | [DEPENDENCY_AUDIT](DEPENDENCY_AUDIT.md)；静态无环，运行时双向协作单独记录 |
| 五项优先债务及最小迁移路径 | [TECHNICAL_DEBT](TECHNICAL_DEBT.md) |
| 七个项目的能力、维护、许可证、成本及阶段决策 | [OPEN_SOURCE_ARCHITECTURE_RESEARCH](OPEN_SOURCE_ARCHITECTURE_RESEARCH.md)，固定 SHA 源码/许可核查 |
| 插件注册、启停、历史、异步、失败与共享基础能力 | [PLUGIN_PILOT](PLUGIN_PILOT.md)、`tests/test_plugins.py` |
| Ruff 基线、架构防护、统一检查与 CI 接入 | [QUALITY_GATES](QUALITY_GATES.md)，既有 CI 安装开发依赖并调用 verify |
| 后续新增功能的八项问题、边界、验证与回滚 | [AGENTS.md](../../AGENTS.md) |

## 保留限制

本轮没有发布、提交付费推理或迁移业务数据；现有生产服务是否运行此版本不在本验收结论内。
开关是显式准入控制，已受理任务继续执行；进程内切换不是多进程热配置。
插件为受信任的同进程扩展，不是恶意第三方代码沙箱。前端共享状态、存储私有 API 和多套版本模型
已经审计并列出后续步骤，没有用本轮试点宣称这些债务全部解决。

根工作区初始无 Git 提交；修改前快照和 SHA256 保留，最终文本差异在
`work/architecture-governance-20261020/changes.diff`。回滚应逐文件合并，不能整包覆盖或删除业务数据。
