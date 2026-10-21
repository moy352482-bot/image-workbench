# 图片任务性能记录与分析

性能记录只做观测与离线汇总，不决定任务是否重试，也不改变原有 Codex CLI 的命令、参数、调用流程或已存在的 Worker/队列。采集失败不能把成功生成改为失败。本次汇总工具不生成图片，不扫描生产或历史任务。

## 单次执行记录

每次执行单独保存 `data_root/logs/performance/<task_id>--<execution_id>.json`，使用 `schema_version: 1`。记录包括任务、项目、执行、操作、后端、A/B 提示类型、参考图和输出数量、结果、失败类型、时间戳、阶段耗时、测量限制、阶段片段和已观测 Codex 事件。

- `A_plain`：没有参考图片；`B_reference`：含参考图片或基底。
- `status` 为 `success` 或 `failed`；`execution_status` 保留原执行状态。重试是新的 `execution_id`，不能覆盖或合并原失败。
- 未观测阶段为 `null`，只有实际测得的零耗时才能写 `0`。
- `timestamps` 使用带时区的 UTC，用于跨日志定位；阶段耗时使用 monotonic / `perf_counter`，不依赖系统日期。
- `total` 是任务创建到本次 Runner 完成的 UTC 差值。`queue_or_idle` 包含创建后尚未发起执行的空闲时间，不能直接称为队列调度延迟。系统时钟调整可能影响两者；负值保留在原始记录，汇总时列为无效值并排除。
- `runner` 为本次 Runner 的 monotonic 耗时；任务重试时，创建至完成总时长可能包含先前等待或尝试，比较时应核对批次说明。

`durations_seconds` 包含 `prepare`、`codex_launch`、`codex_execution`、`codex_combined`、`artifact_check`、`register`、`total`、`queue_or_idle`、`runner`、`other`。准备、检查与登记可能分多次发生，应累计对应 `phase_segments`；不能直接把阶段首个 start 与最后 end 的差值当作阶段总时长。

## Codex 边界与推断限制

`turn.started` 只是采集器在本机实时观察到的代理边界，不证明模型已经开始推理。当前 CLI stdout JSONL 没有源事件时间戳，观察时间包含输出刷新和轮询延迟；收尾补读到的事件不能充当实时启动边界。

`codex_execution` 包含理解、调度、ImageGen、结果返回及进程退出，不能解释为图片模型计算时间。当前没有图片工具开始/结束事件，`imagegen_tool_seconds` 保持 `null`，无法由 stdout 再拆分模型内部耗时。没有可信实时边界时，`codex_launch` 和 `codex_execution` 都应为 `null`，仅使用可确认的 `codex_combined`。

`codex_combined` 与启动/执行两行重叠，`runner` 也包含各阶段；阶段表所有行不能相加。最终性能文件序列化、Worker 结算和 HTTP 返回可能不在 Runner 测量范围内，具体限制以原始 `measurements.limitations` 为准。

## 明确选择本批样本

创建 manifest，只列本批已存在的性能 JSON 绝对路径。不要用目录扫描把历史批次、校准数据或本地合成测试混进实测。manifest 是样本选择清单，不是实测真实性证明；批次负责人仍需核对后端、执行 ID 和生成结果。

```json
{
  "records": [
    "D:/ChatGPT/图片网站/data/logs/performance/<task_id>--<execution_id>.json"
  ],
  "sample_metadata": {
    "batch": "本轮 10 次执行，A/B 各 5 次",
    "selection": "仅本轮明确列出的真实执行；不含校准、历史与合成数据",
    "conditions": "记录实际模型、输出数量、图片规格、并发和异常情况"
  }
}
```

上例路径和批次说明是格式示例，须替换为实际情况。没有样本时使用 `"records": []`，输出会明确显示“尚无数据”。

```powershell
python scripts/performance_report.py --manifest work/performance-manifest.json --output reports/performance-current
```

工具只读取清单列出的文件；相对路径、重复文件或重复 `task_id/execution_id` 会报错。输出不能覆盖 manifest 或原始记录。输出目录产生：

- `performance_report.json`：完整原始记录、来源绝对路径和 SHA-256、批次说明、整体及 A/B 分组统计、失败列表、测量限制与建议。
- `performance_summary.md`：逐次任务明细、中文总时长与阶段表、失败执行、短中长期建议。逐次表保持 manifest 顺序，直接展示 task_id、A/B、结果、总时长及五个阶段。

## 统计口径

`overall` 和 `groups.A_plain` / `groups.B_reference` 均分为 `success` 与 `failed`，失败耗时不会进入成功均值。每个阶段提供 `known_count`、`missing_count`、`invalid_count`、`mean`、`median`、`min`、`max`。没有已知值时，统计值为 `null`。

阶段加权占比 `weighted_share` 使用**相同样本**的分母：仅选择该阶段已知且 `total` 为有效正数的记录，再计算 `sum(阶段秒数) / sum(这些记录的 total 秒数)`。`share_sample_count`、`share_numerator_seconds` 与 `share_denominator_seconds` 让分母可复查；不使用所有任务总时长稀释缺失阶段，也不对各任务百分比简单平均。

例如：甲执行总时长 100 秒，登记 10 秒；乙执行总时长 900 秒但登记未知。登记占比为 10/100=10%，而非 10/1000=1%。由于可用样本不同，各阶段占比不保证加总为 100%。UTC 时钟偏移亦可能导致占比异常，应优先查看 monotonic 阶段与 Runner 数据。

建议基于成功样本的加权占比，阈值是本批观察提示，不是验收标准：

- `codex_execution > 70%`：只能表述 Codex 执行整体（理解、调度、ImageGen、返回）主导，不能断言模型是瓶颈。
- `codex_launch > 30%`：建议区分进程创建、初始化和认证，再做冷/热启动对照；报告不修改 CLI 或实现进程复用。
- `register > 20%`：建议按片段核对 staging copy、登记和最终状态保存；报告不改存储实现。
- 登记占比低于 5% 时明确保留现有存储实现，存储优化优先级低；没有证据时不建议为提速减少复制。

10 次执行只能形成描述性基线，不能推导总体可靠性、尾延迟分布或证明 A/B 的因果差异。失败路径单列分析；长期建议只在多批次复现后围绕**现有** Worker/队列开展受控实验，不把它们当作尚未实现的功能。

## 本轮 10 次真实采集的环境口径

本轮采集使用同一台机器、同一磁盘上的项目数据副本。初始副本保留 58 个项目、108 个任务、133 个产物及 Storage 索引规模，用于让查找、检查和登记面对已有数据量；未复制账号、作业和历史 logs。仅本轮明确列入 manifest 的性能记录参与统计，不把副本中的历史任务算作本批执行。

采集覆盖 TaskRunner 到 Codex 再到产物检查和登记的核心链路，不测 HTTP 请求、扣费或远端传输；不能据此推断完整网站响应时间或远端部署表现。具体完成数、成功失败和测量结果以本批 manifest 生成的报告为准。
