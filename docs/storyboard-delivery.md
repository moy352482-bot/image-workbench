# AI 智能分镜规划：本地交付说明

本次在现有 Image Workbench 的脚本区、视频区加入独立分镜规划、审核编辑和逐镜头视频桥接。脚本仍输出 TXT，分镜保存结构化 JSON，视频仍由原 VideoStore / VideoBackend / H3 Worker 链路执行。没有推送、部署、修改生产数据库或调用真实付费模型。

2026-10-20 追加验收：当前工作区已具有上述分镜实现，因此本轮复用并修复边界问题，没有重建第二套分镜或视频系统。补齐排队提示词冻结、语义时长冲突保留、不同分镜候选隔离、放弃/失败后的明确重试，以及原视频表单的画幅和参考图同步。最终完整检查 **543 项，539 通过、4 项环境限制跳过**。

## 1. 复用的模块

| 原模块 | 本次用途 |
| --- | --- |
| ScriptService、原脚本与剧本 Skill、模板系统 | 继续生成和归档 TXT，完成后发出独立分镜阶段意图 |
| ScriptModelRegistry、DemandRouter、CodexTextAdapter | 独立选择分镜模型及思考档位；推荐仅先执行 Python 规则 |
| IdentityStore | 使用原积分冻结、结算、退款及幂等账本 |
| ProjectManager、WorkspaceProjects、StorageManager | 复用项目归属、工作空间隔离、Task、Artifact、不可变文件及来源链 |
| VideoStore、原 VideoBackend / H3 Worker | 生成指定镜头视频；没有新增第二套视频执行器 |
| 原脚本页、pipeline、视频表单、任务卡 | 新增分镜选项和清单面板，保留原导航与视频生成操作 |

已有 VideoStore 的手动镜头是视频执行任务卡。本次清单管理的是可编辑的规划版本，两者通过稳定 `storyboard_shot_id` 连接。选择镜头会创建或复用原视频任务草稿，后续仍在原任务卡明确点击生成。

## 2. 独立 Storyboard Skill

新增 `skills/storyboard-planning/SKILL.md`，只接受已归档脚本版本与规划参数，返回 JSON。它明确禁止调用图片、视频、Shell、网络或其他 Agent。素材正文作为不可信数据输入，不能改变工具权限和计费规则。

脚本 Skill 未被改成分镜 Skill。用户勾选“同时生成分镜规划”后，两阶段技术上依然独立，各有自己的任务 ID、模型选择和费用。

## 3. Storyboard / Shot 数据

Storyboard 包含稳定 ID、脚本 Artifact / Version、目标毫秒数、模型/思考强度、完整提示词、创建时间、版本号、审核状态、当前镜头、已替代镜头和变更记录。每个正式版本登记独立 JSON Artifact，并连接前一版 Artifact。

Shot 包含稳定 `shot_id`、独立 `order`、场景、标题、画面、动作、景别、运镜、情绪、对白、旁白、图片/视频提示词、画幅、视频模型、引用 Artifact、首尾帧、来源 Shot ID。

两个时间字段独立：`narrative_duration_ms` 是成片占用，`generation_duration_ms` 是模型素材长度。界面按秒显示，Python 按整数毫秒验证。

## 4. 剧情与时长

真实模型提示词要求根据建立、推进、转折、高潮、收束以及动作、对白、情绪、景别、运镜和场景切换安排镜头。镜头数不固定，不按文字数量平均分配。模型输出经过 Python 结构、毫秒合计、引用范围和视频能力校验。

默认 Mock 是可复现的离线规则实现：按场景与事件分段，估算动作完成和对白阅读的最低时间，对转折、情绪分配额外时间，超出单次素材上限时分段。它用于流程验收，不能据此认定真实 AI 的创作质量。

## 5. 时长冲突

目标不足时保留剧情，返回 `duration_conflict`，包括目标、合理时长、实际合计、原因和建议。用户可增加目标、修改镜头或压缩剧情后重新规划。冲突未解决不能整体审核通过；保存、排序不会无故清除已有语义冲突。

素材能力从 VideoStore 配置读取。当前 H3 为 4–15 秒、1 秒步长。例如叙事 3 秒可生成 4 秒素材，界面明确提示。本次不执行剪辑；合并后超过上限会提示分段生成或调整。

## 6. 页面与交互

脚本页和视频工作台脚本步骤都有“同时生成分镜规划”，默认关闭。启用后分别展示脚本和分镜模型、思考档位、预计积分及阶段状态。历史脚本可针对选定版本独立规划，不重新生成脚本。

视频步骤新增清单、目标/合计时长、镜头数量、历史版本和镜头编辑区域。支持参考图、首尾帧、现有视频关联与过期提示。局部规划通过复选框多选，生成候选后显示预览，用户接受才产生正式新版本。

## 7. 编辑与来源

新增、删除、修改、拆分、合并、拖动排序、保存和审核均生成新版本。普通修改及排序保留 Shot ID；拆分与相邻合并创建新 ID，记录 `source_shot_ids`，旧镜头保留在历史及 `retired_shots` 中。不会删除旧媒体。

局部规划支持非连续选择，分别替换每个连续小组，未选镜头的内容、身份及相对顺序不变。过期预览不能覆盖后来手动保存的新版本。

## 8. 图片与视频资产

所有规划引用使用 Artifact ID，不使用本机绝对路径或文件名。已有上传图首次进入分镜时登记 Artifact；从图片模块导入时保留原 Artifact 身份。读取和提交前检查归属、存在性、大小与哈希。

桥接时首尾帧进入 I2V，多图参考进入 R2V，无图进入 T2V。H3 不支持首尾帧加额外参考图的组合，因此明确提示用户调整。提交视频前再次检查原引用，缺失时不借用副本继续生成。

成功视频登记 MP4 Artifact，绑定 Storyboard ID、版本、稳定 Shot ID 和镜头内容指纹。修改画面或提示词后旧视频标记 `outdated`；只重排不会过期。视频原文件和下载链接保留。

## 9. 两阶段任务与恢复

脚本先完成 TXT、Artifact、Version 归档与本阶段结算，再启动分镜。分镜失败只影响自身任务，原脚本继续可读可用；失败分镜可以从该历史脚本重新发起。

请求有持久化幂等 ID 和跨进程执行锁。刷新/关闭浏览器不会重复调用。重启扫描脚本阶段意图、分镜队列、模型完成凭据及积分 outbox；有候选或完成凭据时仅恢复归档，没有结果凭据的中断任务为 `uncertain`，不自动重复推理。取消退款前中断、完成后结算前中断均可在启动恢复时补结算。

## 10. 模型与费用

分镜配置位于 `config/storyboard_pricing.json`，模型和思考强度分别定价，不读取脚本价格作为默认值。当前 `mock_enabled=true`、`real_enabled=false`、`free_test_enabled=false`、价格表为空。

真实模式必须具备已配置价格、可用模型、足够余额、匹配参数的有效报价和用户确认。真实免费测试还需显式开启免费测试并设置 0 价格。脚本原价未改；两阶段报价分开展示。所有本次真实模式分支测试均替换模型为 Mock。

## 11. 文件范围

新增：

- `image_workbench/storyboard_models.py`、`storyboard_planner.py`、`storyboard_service.py`、`storyboard_web.py`
- `image_workbench/contracts/storyboards.py`
- `skills/storyboard-planning/SKILL.md`、`config/storyboard_pricing.json`
- `image_workbench/web_static/storyboard.js`、`storyboard.css`
- `tests/test_storyboard_http.py`、`test_storyboard_planner.py`、`test_storyboard_recovery.py`
- 本交付说明、`docs/storyboard-api.md`、`docs/storyboard-testing.md`

增量接入：

- `script_service.py`、`contracts/scripts.py`：两阶段意图、独立报价、脚本完成后的依赖触发
- `composition.py`、`accounts_web.py`：装配、路由、项目活动任务保护
- `storage.py`、`atomic_files.py`、`project_manager.py`、`workspace_scope.py`、`workbench_state.py`、`http_api/artifacts.py`：JSON/MP4 Artifact 及正确分类、访问；原子写临时文件不参与任务枚举，Windows 短暂占用有限重试
- `video_store.py`、`pipeline_service.py`：原图片身份、单镜头桥接、视频产物登记
- `script_web.py`、`video_web.py`：页面资源版本包含新增分镜文件
- `web_static/scripts.js`、`pipeline.js`，`web_templates/scripts.html`、`video.html`：现有页面集成
- `scripts/verify.py`：分镜模块检查覆盖新增测试

仓库同时有其他聊天的架构、脚本素材和素材 UI 改动。上述列出本次接入触点，不把同期其他改动认领为本次功能；没有打包提交整个未跟踪工作区。

## 12. 验证与证据

自动化测试映射与复现命令见 [storyboard-testing.md](storyboard-testing.md)。测试均使用临时数据、Mock 文本和模拟视频 worker。浏览器联调覆盖两阶段生成、历史版本、编辑、拆合、排序、局部预览确认及单镜头草稿绑定；浏览器没有发送真实视频 `/run` 请求。

本轮最终完整检查为 543 项：539 通过、4 项环境限制跳过，退出码 0，耗时 207.486 秒；检查期间源码指纹未变化。分镜后端专属 60 项通过，公共组件检查同时运行新增的 11 项分镜前端行为用例，架构为 75 模块、378 导入、0 违规。

本轮日志、源码指纹与浏览器截图保留在 `work/storyboard-followup-20261020/`。浏览器在独立临时数据中验证了：只改画幅产生新版本、T2V 切换 I2V 保存首帧 Artifact、不同分镜候选切换后恢复、放弃候选后相同输入产生新任务，以及 v3 指定镜头进入原 VideoStore 草稿。控制台无警告或错误，未启动真实视频任务。

初版两阶段 smoke 的 TXT 与分镜均完成，JS/HTTP 错误均为 0；其证据仍保留在 `work/storyboard-planning-20261020/`。初版清理曾被自动批准审查拒绝（仅返回“策略阻止”），`C:/Users/win/AppData/Local/Temp/iw-storyboard-qa-vv69bhjd` 中的旧 Mock 测试数据因此保留，本轮没有重复清理该目录。

追加修改文件为 `storyboard_planner.py`、`storyboard_service.py`、`web_static/storyboard.js`、`tests/test_storyboard_followup.py`、`tests/frontend/storyboard.test.js`、`tests/test_frontend_modules.py`、`scripts/verify.py` 及三份分镜文档。脚本模型、路由和价格配置与初版备份的 SHA-256 均一致。

## 13–14. 实现与 Mock 边界

| 项目 | 状态 |
| --- | --- |
| 数据模型、API、账号/Workspace 边界、版本、稳定 ID、编辑、审核、预览确认 | 实际实现 |
| 两阶段依赖、队列、幂等、重启恢复、独立计费与账本 | 实际实现；测试使用临时账本 |
| 图片 Artifact 关联、原视频草稿、视频产物 Artifact、outdated | 实际实现；视频成功回执用模拟 worker 验证 |
| Codex JSON 适配器、独立模型/思考档位、Skill 提示词 | 已接入；未调用真实模型验证创作质量 |
| 默认规划内容 | Mock 规则生成，界面标明 Mock、0 积分 |
| H3 / GPU 实际生成、真实素材质量和生产部署 | 本次未执行 |

## 15. 待用户审批

正式分镜价格、是否开放真实付费或免费测试、真实模型内容质量验收，以及上线部署由用户决定。清单采用现有页面内的最小可用布局，视觉样式可在上线前审核。未开发剪辑器、自动批量生图或自动批量视频生成；审核分镜也不会自动执行媒体任务。
