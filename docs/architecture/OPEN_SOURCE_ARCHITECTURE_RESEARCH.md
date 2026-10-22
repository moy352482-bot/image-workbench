# 开源架构研究与采用决策

核查日期：2026-10-20。依据官方文档、GitHub API、固定 commit 的实际实现和 LICENSE，
不是仅阅读 README。源码快照、完整 SHA、原始链接、时间及文件摘要保存在
`work/architecture-governance-20261020/upstream/*/evidence.json`；下载源码仅用于审阅，没有并入业务代码。
下表维护状态是当日观察，版本不是永久的“最新版”承诺。

| 项目 / 仓库 | 代码许可证 | 核查时稳定发布证据 | 默认分支提交日期（UTC）/ 审阅 SHA | 本轮选择 |
| --- | --- | --- | --- | --- |
| [Pluggy](https://github.com/pytest-dev/pluggy) | [MIT](https://github.com/pytest-dev/pluggy/blob/0224f79fc75e/LICENSE) | [PyPI 1.7.0，10-15](https://pypi.org/project/pluggy/1.7.0/) | 2026-10-20 / `0224f79fc75e` | 借鉴接口与显式注册，不增加依赖 |
| [Import Linter](https://github.com/seddonym/import-linter) | [BSD-2-Clause](https://github.com/seddonym/import-linter/blob/857ae28bc9cb/LICENSE) | [PyPI 2.15，09-04](https://pypi.org/project/import-linter/2.15/) | 2026-10-13 / `857ae28bc9cb` | 保留现有 AST 契约，分包后重新评估 |
| [Ruff](https://github.com/astral-sh/ruff) | [MIT](https://github.com/astral-sh/ruff/blob/e02754d1fbc6/LICENSE) | [0.17.1，10-15](https://github.com/astral-sh/ruff/releases/tag/0.17.1) | 2026-10-20 / `e02754d1fbc6` | 固定 0.17.1 作为开发检查工具 |
| [Prefect](https://github.com/PrefectHQ/prefect) | [Apache-2.0](https://github.com/PrefectHQ/prefect/blob/817bc93b6e82/LICENSE) | [3.8.8，10-06](https://github.com/PrefectHQ/prefect/releases/tag/3.8.8)；另有 3.8.9.dev7 预发布 | 2026-10-19 / `817bc93b6e82` | 不替换现有 TaskRunner/Worker |
| [Stevedore](https://github.com/openstack/stevedore) | [Apache-2.0](https://github.com/openstack/stevedore/blob/849be420e44d/LICENSE) | [PyPI 5.9.1，08-20](https://pypi.org/project/stevedore/5.9.1/) | 2026-10-07 / `849be420e44d` | 当前无独立发行插件发现需求，暂不引入 |
| [Dependency Cruiser](https://github.com/sverweij/dependency-cruiser) | [MIT](https://github.com/sverweij/dependency-cruiser/blob/60f8a7ef72cb/LICENSE) | [18.7.0，10-19](https://github.com/sverweij/dependency-cruiser/releases/tag/v18.7.0) | 2026-10-19 / `60f8a7ef72cb` | 原生全局脚本阶段不引入 |
| [Temporal Python SDK](https://github.com/temporalio/sdk-python) | [MIT](https://github.com/temporalio/sdk-python/blob/3fd033971d88/LICENSE) | [1.34.0，09-30](https://github.com/temporalio/sdk-python/releases/tag/1.34.0) | 2026-10-19 / `3fd033971d88` | 长流程实际规模触发后再评估 |

七个仓库核查时均未归档，近期有提交；这只能支持“仍有维护活动”，不能保证未来支持或无缺陷。
默认分支源码可能晚于稳定包。本轮只安装并执行了 Ruff；没有对 Prefect/Temporal 的部署吞吐作基准测试。

## Pluggy：接口思想适合，hook 扇出暂不需要

官方 [插件接口文档](https://pluggy.readthedocs.io/en/latest/api_reference.html) 给出 hookspec、hookimpl、
注册、阻止注册和注销。实际
[`PluginManager.register/_verify_hook`](https://github.com/pytest-dev/pluggy/blob/0224f79fc75e/src/pluggy/_manager.py)
检查实现参数是否符合 spec，收集 HookCaller；`_callers.py` 负责调用链及 wrapper/firstresult。
它管理的是内存对象与调用，不会自动管理任务生命周期、事务、隔离或异步队列。

收益是多个独立扩展订阅同一事件并统一校验契约；成本是 hook 顺序、返回值聚合、异常传播和
与持久任务生命周期的额外映射。当前脚本生成是一项明确能力对应一个服务，Protocol + 显式注册更直接。
图片、脚本、分镜适合能力接口；Credit/Storage 不适合通过多个 hook 修改。
未来出现多个独立供应方扩展同一生命周期点时再试 Pluggy。无论采用哪个库，都只注入公开端口；
同进程 Python 插件不是安全沙箱，导入约束和审阅仍必要。

## Import Linter：能表达现有规则，但避免维护两套事实

[官方契约文档](https://import-linter.readthedocs.io/en/stable/contract_types/) 列出 forbidden、protected、
layers、independence 和 acyclic siblings。
[`forbidden.py`](https://github.com/seddonym/import-linter/blob/857ae28bc9cb/src/importlinter/contracts/forbidden.py)
实际区分直接链和最短间接依赖链；`independence.py` 用单层 sibling 模块检查依赖；
`layers.py` 支持层约束，`acyclic_siblings.py` 通过图分析报告需断开的边。

它适合 CI，平铺模块也能使用，收益是成熟图分析和违规链解释。成本是 Grimp 等开发依赖、规则映射，
以及对现有合法间接依赖的建模。当前已有基于 `architecture.json` 的完整模块归属、允许方向和循环检查；
再手工维护另一份 INI/TOML 容易漂移。先保留现有检查与负例测试。下一阶段模块形成子包时，从现有配置
生成 forbidden/layers 契约，并验证两套结果一致，再替换旧检查相关部分；不能通过宽泛 ignore 放行业务互相依赖。

## Ruff：现在采用增量质量门槛

[配置文档](https://docs.astral.sh/ruff/configuration/) 与
[formatter 文档](https://docs.astral.sh/ruff/formatter/) 分别说明 lint 与格式化配置。
[`linter.rs`](https://github.com/astral-sh/ruff/blob/e02754d1fbc6/crates/ruff_linter/src/linter.rs) 的
`check_path/lint_only/lint_fix` 调用 token/AST 检查并区分检查与修改；
[`configuration.rs`](https://github.com/astral-sh/ruff/blob/e02754d1fbc6/crates/ruff_workspace/src/configuration.rs)
处理规则选择及项目配置。仅调用稳定 CLI，不依赖其内部 Rust API。

收益是未定义名、可疑绑定、错误控制流等自动检查；成本是处理历史诊断和固定版本升级。
兼容现有 unittest/FastAPI，不加入生产依赖。已记录 134 条旧诊断，启用 `E4/E7/E9/F/B`，
只允许相同问题代码与数量保留，新增诊断失败。格式化可用于新文件或明确选定范围，不全库重排。
实施阶段：本轮已完成安装、配置、基线、增量检查和统一入口接入。

## Prefect：编排与观测有价值，不能替代业务幂等

[官方 Task 文档](https://docs.prefect.io/v3/concepts/tasks) 描述 Flow/Task 状态、submit、retry、cache、
超时和后台执行。实际
[`task_engine.py`](https://github.com/PrefectHQ/prefect/blob/817bc93b6e82/src/prefect/task_engine.py)
的 `handle_retry/can_retry/transaction_context` 处理重试条件、AwaitingRetry 状态与结果事务；
`tasks.py` 定义任务参数，`transactions.py` 管理结果事务。

收益是多阶段编排、观察和可复用缓存。引入成本包括服务/结果存储、Worker 运维、依赖升级及现有
Task/JSON/SQLite/outbox 的状态桥接；本地简化运行可行，但要获得持久观测和分布式调度仍需相应基础设施。
其缓存和事务并不自动把外部模型调用、文件归档与本项目余额更新变成一个原子事务。
现有单机/远程 GPU 工作台已有关键恢复路径，暂不替换。后续若需要复杂生产流程，
先在只读报告或无付费副作用的编排上验证，再决定是否值得迁移。

## Stevedore：独立安装包发现才是它的主要价值

[官方加载模式](https://docs.openstack.org/stevedore/latest/user/patterns_loading.html) 区分 Driver、Hook、
Extension 与 Dispatch。
[`ExtensionManager.list_entry_points/_load_one_plugin`](https://github.com/openstack/stevedore/blob/849be420e44d/stevedore/extension.py)
实际缓存 entry point 列表、执行 `ep.load()` 并可在加载时实例化。
`driver.py` 支持单驱动选择，`enabled.py` 提供启用筛选。

收益是由独立 Python distribution 注册的扩展不需修改宿主名单；成本是包元数据、版本兼容、
导入副作用、加载失败与冲突治理。与当前 Protocol 可组合，但不能阻止插件访问内部状态，也不负责保留历史。
本项目内置功能都在 composition 启动注册，尚无插件发行/发现问题。本轮不引入；
只有存在独立安装和升级的第三方插件时再评估，不为“可扩展”预先增加动态加载。

## Dependency Cruiser：先有真正的前端模块图

官方仓库的 [规则说明](https://github.com/sverweij/dependency-cruiser/blob/60f8a7ef72cb/doc/rules-reference.md)
描述 forbidden/allowed、circular 和路径/层规则。
[`src/extract/index.mjs`](https://github.com/sverweij/dependency-cruiser/blob/60f8a7ef72cb/src/extract/index.mjs)
递归提取可解析依赖；
[`src/validate/index.mjs`](https://github.com/sverweij/dependency-cruiser/blob/60f8a7ef72cb/src/validate/index.mjs)
将规则应用到模块、依赖及目录，生成带 severity 的违规结果。

收益是 JS/TS 模块导入图和 CI 门槛；成本是 Node 包管理、解析器和规则维护。
当前页面没有 ES module import 图，关键耦合是 `window.IWVideo.state` 和 DOM 事件，导入扫描不能直接发现它们。
因此本轮不引入，保留现有组件/浏览器检查。迁移 ES modules 后，先上 no-circular 与禁止组件导入页面入口，
再根据实际模块分层扩展规则。

## Temporal：持久化 Workflow 需要配套服务与幂等 Activity

[Python 开发指南](https://docs.temporal.io/develop/python) 与
[错误处理文档](https://docs.temporal.io/develop/python/best-practices/error-handling) 说明 Workflow/Activity、
超时、重试及心跳。
[`_workflow_instance.py`](https://github.com/temporalio/sdk-python/blob/3fd033971d88/temporalio/worker/_workflow_instance.py)
消费 WorkflowActivation、标记 replay 并应用 timer/activity 等事件；
[`_activity.py`](https://github.com/temporalio/sdk-python/blob/3fd033971d88/temporalio/worker/_activity.py)
通过 bridge 发送 heartbeat，读取恢复 details；
[`common.py:RetryPolicy`](https://github.com/temporalio/sdk-python/blob/3fd033971d88/temporalio/common.py)
默认 maximum_attempts=0 表示不限制次数，可配置不可重试错误。

收益是跨进程、长寿命工作流历史与定时/人工等待恢复；成本是 Temporal Service、数据存储与运维、
确定性工作流代码/版本策略、现有 Worker 与账本迁移。SDK 本身不等于持久服务器，Activity 重试也不等于付费调用恰好一次。
若将来采用，生成 Activity 仍必须沿用 attempt_id/原回执，结果不明只对账，不能采用默认重试重放付费推理。
当前 H3 单镜已有小时级轮询和恢复，个人工作台先增强现状；跨主机多日流程成为验证过的需求后再试点。

## 许可与实施边界

上述 LICENSE 已逐一读取。MIT、BSD-2-Clause、Apache-2.0 的工具使用与接口借鉴均无需复制实现；
分发源码或二进制时仍须按相应许可保留声明，Apache 项目还需检查适用的 NOTICE。
本轮没有复制上游实现到业务模块，也没有引入云服务账号或费用。未来实际代码复用需针对所选版本和依赖再次核对，
不能仅用本表替代具体分发审查。

采用顺序：Ruff 与现有契约防护 → 显式脚本插件 → 公开服务端口/前端状态解耦 →
按真实独立发行需求评估 Pluggy/Stevedore → 按真实编排瓶颈评估 Prefect/Temporal。
