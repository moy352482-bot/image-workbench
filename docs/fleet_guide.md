# Luna 舰队批量实测

本轮目标是至少 200 张新生成、通过文件校验并登记的真实图片。Luna 子代理负责用例准备、恢复机制和存储性能检查；真实请求走原 Codex 后端，保持同一数据目录内串行执行。

**当前状态：已按用户要求停止。** 实际完成 33 张主测图片，另 1 项在停止时中断；未完成原 200 张目标。完整结果见 [总结与修复记录](../reports/fleet_summary.md)。`reports/fleet-200/stopped_by_user.json` 保留停止决定，以下原舰队命令现在会拒绝自动续跑；重新生图须有新的明确授权与计划。

## 执行和续跑

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/fleet_run.py --allow-image-generation
```

该命令会使用当前 ChatGPT 登录的生图额度，固定请求 `gpt-6-luna`。计划为 10 份主测清单、2 份备用清单，每份 20 个任务。每个任务要求一张图片，覆盖纯生成、参考图生成、连续编辑和历史分支。达到至少 200 张后，在分片边界停止，因此最终数量可能略多于 200。校准图、旧图和本地合成夹具均不计入目标。

同一命令再次运行时，会读取 `reports/fleet-200/` 的原进度。已完成任务不会重调模型；终态执行会按原 execution ID 恢复报告。原执行仍在运行、记录缺失或状态不能确认时，会保留现场并停止后续提交。不要删除进度文件后从头重跑来绕过这种状态。

遇到明确的额度、鉴权或能力限制，或连续三项同类失败时停止后续提交。原始失败结果不被复测覆盖。脚本同时使用舰队锁和分片锁，避免两个进程改写同一份进度。

单份清单也可通过 CLI 执行或恢复：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m image_workbench batch --manifest tests/fleet_cases/01_product/suite.json --backend codex --model gpt-6-luna --allow-image-generation
.\.venv\Scripts\python.exe -X utf8 -m image_workbench batch --manifest tests/fleet_cases/01_product/suite.json --backend codex --model gpt-6-luna --allow-image-generation --resume-directory "原批次报告目录"
```

不带 `--resume-directory` 的单份 batch 命令代表创建新批次，会重新生成。正在进行的舰队任务应使用第一条舰队命令续跑。

## 查看结果

- `reports/fleet-200/fleet_results.json`：每项完成后更新的数量、耗时、错误和图片清单。
- `reports/fleet-200/fleet_results.csv`：可用 Excel 打开的任务明细。
- `reports/fleet-200/batches/`：每个分片的 manifest、稳定 ID 进度、JSON/CSV 和原始日志索引。
- `data/projects/`：原工作台的数据和新图片；在 `http://127.0.0.1:7860` 中按项目查看。

运行以下只读核对，生成可点击原图的本地图册：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/fleet_report.py
```

它检查登记文件的大小、SHA256、可解码性、历史来源、真实执行状态、模型命令和缓存源图，输出 `verified_artifacts.json` 和 `gallery.html`。图册是生成时的快照，后台继续跑图后可再次更新。图片保持待人工审核，不因批测成功自动通过审核。

## 本轮已处理的问题

校准请求发生过“图片已保存，但 Luna 最终回复没有路径”。后端现在只在退出成功、结构化回复身份正确、CLI 日志有唯一已完成会话、该会话专属目录恰有一个新产物时尝试找回；仍执行目录、重解析点、时间、大小、解码及哈希校验。不存在全局“找最新图片”行为，不重新调用图片工具。原 `response.json` 保留，恢复依据写入 `artifact_recovery.json` 并进入批测警告和日志索引。

批测新增持久化进度与幂等 execution ID。存储的图片 ID 查询新增经完整元数据树 stat 签名校验的实例内索引；外部写入会重建，JSON 仍是权威来源。批次收尾按所属任务集中读取清单，减少重复全库查询。性能对比见 `reports/fleet_storage_optimized.md`，该报告使用合成夹具，不代表生图服务端渲染速度。

网页历史列表现在直接显示各任务的首张输出缩略图；长提示词在列表中省略显示，详情保留全文。选中状态待确认的任务仍会只读轮询，CLI 批测归档完成后可自动显示结果。静态脚本与样式带版本号，普通刷新即可加载更新。
