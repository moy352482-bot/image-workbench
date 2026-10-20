# Storyboard 增量接口（本地实现契约）

所有 HTTP 接口复用账号、Workspace、CSRF 边界，ID 为 32 位小写十六进制。时间为整数毫秒。

## 规划与脚本联动

`GET /api/storyboards/config` → `{models, mock_enabled, real_enabled, video_models}`；模型档位的 price 属于分镜独立配置，null 不可执行。

规划 options：
```json
{"mode":"mock","selection_mode":"recommended","target_duration_ms":60000,"aspect_ratio":"16:9","visual_style":"","characters":"","scenes":"","requirements":"","reference_artifact_ids":[],"video_model_id":"h3"}
```
可手动传 `model_id`、`thinking_level`，可传 `video_project_id`。Mock 0 积分，真实模式默认关闭。

`POST /api/storyboards/quote`：options + `source_script_version` → `{model_id,display_name,thinking_level,price,can_generate,pricing_status,reason,shortfall,quote_signature}`。

`POST /api/storyboards/tasks`：同上 + `request_id,quote_signature,confirmed:true`，返回独立 task（含 task_id/storyboard_id/status）。后台执行，不等待浏览器保持打开。
`GET /api/storyboards/tasks/{task_id}`；`POST .../run {}`；`POST .../cancel {}`。

现有 `/api/scripts/quote`、`/api/scripts/tasks` 接受可选 `storyboard_options`。报价额外返回 `storyboard_quote`；脚本报价指纹绑定两阶段参数。提交带 `storyboard_confirmed:true`。不传选项保持原行为。完成 TXT/Artifact/Version 后服务端启动分镜；失败写入独立任务，不改变脚本完成状态。前端不轮询后再提交新规划请求。

## 分镜版本

`GET /api/storyboards?video_project_id=...` 或 `?subproject_id=...` → `{storyboards:[...]}`。
`GET /api/storyboards/{id}?version=N` → 完整版本及 `{versions:[{version,status,created_at,change_reason}],video_project_id,videos:[...]}`。
记录：storyboard_id/source_script_artifact_id/source_script_version/target_duration_ms/status/shots/retired_shots/version/model_id/thinking_level/prompt/created_at/duration_conflict。
Shot：shot_id/order/scene_id/title/narrative_duration_ms/generation_duration_ms/duration_reason/visual_description/action/camera_framing/camera_movement/emotion/dialogue/voiceover/reference_artifact_ids/start_frame_artifact_id/end_frame_artifact_id/image_prompt/video_prompt/aspect_ratio/video_model_id/status/source_shot_ids。
状态：draft/awaiting_review/approved/revision_required。时间冲突为 null 或 `{target_duration_ms,reasonable_duration_ms,reason,suggestions}`。

`POST /api/storyboards/{id}/edit`：
```json
{"request_id":"...","expected_version":1,"operation":"update","shot_id":"...","changes":{"title":"..."},"reason":"手动编辑"}
```
操作：`add`（changes）、`update`（shot_id/changes）、`delete`（shot_id）、`split`（shot_id/parts:[changes,changes]）、`merge`（shot_ids:[相邻ID,...]）、`reorder`（shot_ids:[完整新顺序]）、`save`（可选 target_duration_ms）、`approve`、`revision_required`。
每次生成新版本，旧版本只读；split/merge 新 ID，旧镜头置入 retired_shots，重排保留 ID。所有操作均幂等、expected_version 防止并发覆盖。
仅标题、引用、画幅或顺序变化时，保留已有语义时长冲突；数值合计正确不代表模型报告的对白/动作冲突已解决。时长或实际叙事内容调整后重新校验。

`POST /api/storyboards/{id}/replan/quote`：options + `expected_version,shot_ids:[],reason`。空 shot_ids 代表全局规划。
`POST /api/storyboards/{id}/replan`：同上 + `request_id,quote_signature,confirmed:true` → task。任务完成含 `preview`（完整候选版本），不更新正式版本。
`POST /api/storyboards/{id}/accept`：`{request_id,task_id,expected_version}`；只有此接口接受预览。
局部选择支持非连续镜头。连续小组分别在原位置替换，候选必须携带该组选中 ID 的 `source_shot_ids`，不能跨越中间未选镜头或遗漏某组。未选镜头的内容和相对顺序保留；`order` 根据当前序列重新计算。
浏览器请求和候选缓存按账号、Workspace、视频项目及 Storyboard ID 隔离。放弃候选或收到 failed/cancelled 终态后，下次明确提交使用新 request_id；网络结果未知或 uncertain 状态保留原 request_id，防止重复调用。

## 图片与视频

`GET /api/storyboards/{id}/assets` → `{assets:[{artifact_id,title,url,...}]}`，解析当前项目已有图片（含 video 项目参考图的 Artifact 映射）。缺失图片提示，不替换。
`POST /api/storyboards/{id}/shots/{shot_id}/video-draft`：`{request_id,expected_version,resolution:"720p",allow_draft_test:true}`。
仅将明确版本镜头装入现有 VideoStore，返回原视频 shot（含 estimated_credit），不执行视频。用户随后确认原 `/api/video/projects/{pid}/shots/{sid}/run`。未经审核需明确 allow_draft_test。超时长阻止，并提示分段或调整；首尾帧映射 I2V，其余参考映射 R2V，无图 T2V。
视频 shot/result 绑定 `{storyboard_id,storyboard_version,storyboard_shot_id}`；视频成功登记 MP4 Artifact。`videos` 含原 shot/status/output_url/artifact_id/outdated，按稳定 ID 和内容指纹比对（重排不算过期）。
原 VideoStore 中 `shot_id` 是视频任务卡 ID；`storyboard_shot_id` 才是规划镜头的稳定身份。图片导入保留原 Artifact ID。创建视频草稿、提交实际视频任务时均检查引用图片是否存在及内容哈希，缺失时阻止，不以缓存副本替代。H3 首尾帧模式不能同时携带额外多图参考，明确拒绝而不静默丢弃。

界面复用当前布局。脚本区增加复选框和独立规划参数、阶段费用；历史脚本可选择具体版本规划。视频区清单/编辑/拖动/拆分合并/预览确认/历史版本；选中镜头在现有视频表单带入字段，生成仍走原链路。
绑定镜头后，原视频表单的标题、提示词、生成时长、画幅、模式与有序参考图修改先保存为新分镜版本，用户核对审核或明确草稿测试后再创建视频草稿。视频资产 ID 转回 Artifact ID；I2V 映射首/尾帧，R2V 映射参考列表，T2V 明确清空引用。缺失或尚未映射到视频区的原引用不被静默删除，需在分镜编辑区明确调整。

## 执行与恢复

Task 使用 `queued/running/completed/failed/cancelled/uncertain`；生成完成后的 `review_status=awaiting_review` 与分镜本身的 `draft/awaiting_review/approved/revision_required` 分开管理。预览接受不等于审核通过，正式候选仍待审核。

提交按账号与 `request_id` 幂等；参数改变复用 ID 会被拒绝。执行有跨进程任务锁，服务端在 TXT/Artifact/Version 归档后触发第二阶段，浏览器刷新只查询原任务。启动扫描持久化阶段意图和积分 outbox；仅执行从未开始的 queued 任务，已有 JSON 候选或匹配 Codex 完成凭据只做归档，不重调模型。无凭据的中断执行标记 uncertain，保留冻结额度等待核对。完成或取消的 JSON 已落盘但账本未结算时，启动恢复补结算。
任务提交时保存完整规划提示词；排队后执行直接使用该快照，不重新读取 Skill 组装不同提示词，保证真实调用、凭据与分镜版本记录一致。

JSON/MP4 使用临时文件写入、fsync、原子替换后登记 Artifact。分镜版本采用 pending 记录恢复归档，原版本不覆盖。仍有活动分镜任务的项目不能移动或删除。
