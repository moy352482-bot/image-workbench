# 质量防护与基线

统一入口：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 scripts\verify.py
# 小范围迭代仍会先跑 Ruff、架构及公共合同
.\.venv\Scripts\python.exe -X utf8 scripts\verify.py --module scripts
```

顺序为 Ruff → 架构边界 → unittest 单元/服务/HTTP 集成/浏览器回归。
`.github/workflows/verify.yml` 已调用该入口并安装开发依赖，无需新增第二个工作流。
管理中心 `AdminOperations.execute_checks` 和命令行发布 `deploy_release.validate_local` 同样调用该入口。
任何前置检查失败、缺少 Node.js 或验证期间源码改变都会阻止发布；不把单独 `unittest discover` 当作发布通过。
具体提交、CI 和发布状态见 [稳定性修复记录](STABILITY_FIXES.md)，文档不代表生产实时状态。

## Ruff

`requirements-dev.txt` 和 `ruff.toml` 均固定 0.17.1，启用 `E4/E7/E9/F/B`。
`scripts/check_quality.py` 扫描 application、scripts、tests 和 example，排除业务数据、部署副本及 work/reports。
未安装、版本不符、配置无效或无法读基线均失败，不把“没有工具输出”当作通过。

修改前原始诊断在 `work/architecture-governance-20261020/ruff-before.json`；
审核基线为 `tests/contracts/ruff-baseline.json`：134 条诊断、54 个文件。
按相对文件名、规则、消息、问题所在代码及出现次数匹配；不依赖 Git 历史或易漂移的行号，
适用于本工作区初始没有提交的状态。插入正常行不会使旧问题变新，修改问题代码或增加同类诊断则失败。

这不是全库无告警，也不是所有被修改文件都必须清零历史债务。每次运行仍检查全部目标代码；
只有原样保留的已记录问题得到基线豁免。问题消失后旧条目可在对应修复中删除。
禁止为了通过检查自动刷新基线或扩大 ignore；新增豁免须有具体问题、理由和审阅记录。
`test_quality_guard.py` 验证位移、改动、新文件与重复诊断行为。

需要严格检查某个文件或只格式化明确选择的新文件时：

```powershell
.\.venv\Scripts\python.exe -m ruff check image_workbench/script_plugin.py
.\.venv\Scripts\python.exe -m ruff format --check image_workbench/script_plugin.py
```

格式检查作为选定文件的可选命令；本轮没有把全库 formatter 作为门槛，也没有全库自动重排。
上述 format 命令不是本轮已通过声明。

## 架构与行为

`architecture.json` 中每个 Python 模块恰好属于一个组，新增模块必须明确允许依赖。
`plugin_api` 只依赖共享异常；`script_plugin` 只依赖 `plugin_api` 和输入 schema，
禁止导入 Storage、Identity、Provider 实现或 HTTP 框架。通过注入 `ScriptServicePort` 使用应用服务。
普通 Python 同进程不能提供恶意代码隔离，不能把这些规则称为插件沙箱。

已有测试继续保护 HTTP 路径/请求字段、Workspace、积分事务、幂等、恢复、文件安全和前端交互。
新增插件测试使用临时目录、真实本地存储/账本与假文本 Adapter，不读取生产 data、不调用付费模型。
本轮还增加使用当前配置的负例，确保 Core→HTTP、脚本→视频、插件→Storage/HTTP 被拒绝。
2026-10-22 增加私有 `storage._*` 调用防护，以及视频可变状态导出/消费者越过端口的负例。

统一入口在前后核对源码指纹，包含运行配置、Skill、发布脚本、Ruff 配置、开发依赖、CI 工作流及基线；
验证期间代码发生改变则不能据此证明当前版本通过。依赖边界和验证结果仍不能证明真实模型、GPU 或生产环境健康。
