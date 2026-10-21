# 第三阶段：顺序批测与性能记录

批测复用 StorageManager、TaskRunner 和 CodexImageBackend。每份清单允许 1–20 项，按声明顺序执行；不并发、不自动重试生图。每批分配新的项目、任务、执行和报告 ID，同名项目不覆盖历史数据。

## 运行

项目根目录中的零额度完整流程测试：

```powershell
python -X utf8 -m image_workbench batch --manifest tests/manual_batch_cases/suite.json --backend local --image tests/manual_batch_cases/assets/fragrance.webp --reports-root reports/local
```

local 将同一张已有素材作为候选产物复制，只验证工程流程；其比例警告不代表模型表现。

真实 Codex 图片生成与编辑（当前清单最多 12 次图片请求）：

```powershell
python -X utf8 -m image_workbench batch --manifest tests/manual_batch_cases/suite.json --backend codex --allow-image-generation --timeout 600 --reports-root reports/real
```

`--timeout` 是每项 Codex 进程的上限；没有批量自动重试。4 个无效输入在准备阶段被拒绝。命令在 stderr 输出进度，在 stdout 返回最终 JSON；验收全部符合预期且文件终检通过时返回 0，否则返回 1。清单无效或无法保存报告返回 2。

要复跑三项代表用例（纯生成、主体参考、损坏图），把 `--manifest` 改为 `tests/manual_batch_cases/suite_repeat.json`。该清单最多额外调用模型2次。首轮失败的历史编辑另用原 task_id 单独复测，保留失败执行记录。

可在 `batch` 前加 `--data-root D:\测试数据`。保持一个进程写入同一数据目录；批测完成后正常查询/编辑接口依然可用。

## 清单与报告

定义见 [suite.json](../tests/manual_batch_cases/suite.json)；商品来源、哈希和尺寸见 [sources.json](../tests/manual_batch_cases/assets/sources.json)。

字段包含 case_id、project_name、group、operation、prompt、input_images、expected_mode、tags。图片 path 相对清单目录，不能越出该目录。每张图可指定 subject/style/reference。编辑的 base_case 必须指向同项目已列出的任务，使用它的首张输出；基底失败时记录 dependency_failed，不调用模型。

`input_count` 是本任务登记的额外参考图数量；编辑基底单独记录在 `base_artifact_id`，不重复登记为 input。`output_count` 统计实际登记输出，包含故障前已成功登记的部分。

JSON 语法、ID 重复、前序依赖及路径范围会在任何生成前统一验证。无效图片和空 prompt 留到准备阶段，作为明确的负例执行。expected_aspect_ratio 只用于产物核查，不是传给模型的强制尺寸参数；期望比例必须同时写入 prompt。

每批目录 `reports/<类型>/<时间>-<清单名>-<批次ID>/` 包含：

- `manifest.json`：运行时清单快照。
- `batch_results.json`：批次信息、完整结果及汇总；每项完成后刷新。
- `batch_results.csv`：适合表格分析的明细，每项一行。
- `batch_summary.md`：执行成功、符合预期、耗时、分组、问题与建议。
- `cases/<attempt_id>/case.json`、`case.log`、`result.json`：逐项输入声明、错误和结果，即使任务未成功创建也保留。

真实执行目录仍为 `data/projects/<project_id>/tasks/<task_id>/logs/runs/<execution_id>/`：保存 stdout/stderr、execution.json、request.json、命令、进程、Codex 原始响应和候选图片。生成输出经校验后登记在任务 outputs 中；失败不清空已存在内容。

空 prompt 被 StorageManager 在建任务前拒绝，此时 task_id 为 null，以 case_id/attempt_id 追踪。无效图片在任务创建后登记失败，因此通常已有 task_id、没有 execution_id。

## 时间与失败类型

执行记录新增 timing_events，每个阶段包含 phase、started_at、ended_at、seconds。时间点使用带时区 UTC，耗时使用 perf_counter 单调时钟。

| 字段 | 含义 |
|---|---|
| prepare_seconds | 执行器参考图/编辑基底快照；批测行额外包含创建任务及导入输入 |
| codex_seconds | 从启动 Codex 进程前到其退出/超时终止后的时长 |
| backend_seconds | 后端整体调用，包含 Codex 进程及产物收集；与 codex_seconds 嵌套，不相加 |
| validate_seconds | 候选内容解码、大小及哈希核查 |
| register_seconds | 正式登记输出及登记过程中的状态保存 |
| total_seconds | execution.json 中为执行器活动时长；批测行还含外层准备与文件核查 |
| execution_total_seconds | 批测行中单独保存的执行器总时长 |

总阶段结束后才写最终记录，因此总时长不包括该次最终 JSON 写入；批次 wall_seconds 还包含逐项报告写入和整批文件终检。Codex 时间不是服务端图片工具净渲染时间，包含模型调度、推理、生成和协议返回。未进入的校验/登记阶段为 0；未进入 Codex 阶段为 null，不参与其均值。

归档重试追加阶段事件、累计活动耗时，不包含两次调用间空闲时间，也不重新调用图片模型。旧 execution.json 缺少新增字段时仍可读取，缺失计时使用默认值，不能当作已测量数据。

失败分类：prepare_failed、codex_launch_failed、codex_timeout、artifact_not_found、artifact_invalid、register_failed、state_update_failed、unexpected_exception；另有 codex_execution_failed、dependency_failed、integrity_failed、interrupted。原有 status 值继续兼容，failure_type 提供更精确原因。

状态保存失败时保留候选及已有登记；持续写入失败会抛出携带内存记录的 StateUpdateError。批测在独立报告中保留 fallback 记录和可查询的已登记输出。若报告目录本身也无法写入，程序明确失败，不宣称已保存证据。

## 验收口径

执行成功率按实际成功/全部尝试计算；16 项中的4项为负例，全部符合预期时实际执行成功率为75%，正常任务成功12/12、预期符合16/16。故意失败不会被伪装成图片生成成功。

每个成功任务检查目录、登记、真实可解码文件、哈希、基底版本及传入后端的参考快照。整批结束后再核对全部登记输入与输出，检测后续编辑是否改变旧图。比例误差超过2%单列警告，工程成功不等于图片质量合格。

重跑需要比较 case_id、状态、失败类型、输入/输出数量和预期结果；不同批次的项目/任务/图片 ID 应全部独立，旧文件哈希应保持不变。真实模型有随机性，不要求两轮图片像素相同。

本阶段不保证并发写入、断电恢复或长期线上可用性。下一阶段适合先做本地单用户任务页面，并补充后台作业状态与中断恢复入口；公开多用户部署需要单独设计。
