# 分镜自动化验收

在仓库目录运行：

```powershell
.venv/Scripts/python -X utf8 scripts/verify.py --module storyboards
```

最终完整检查：`.venv/Scripts/python -X utf8 scripts/verify.py`，2026-10-20，退出码 0。共 543 项，539 通过、4 跳过，耗时 207.486 秒；检查期间源码指纹未变化。架构检查为 75 模块、378 导入、0 违规。完整日志：`work/storyboard-followup-20261020/final-verify.log`。

4 项跳过均有明确环境原因：两项文件符号链接检查受 Windows 权限/平台限制，两项 Linux Bash 发布模拟需在 Linux/WSL 执行。本次未实际部署。

`tests/test_storyboard_http.py` 通过 `TestClient` 直接调用公开 HTTP 接口。每个测试创建独立临时 Storage、账号、Workspace 和视频项目；脚本使用 Mock 文本适配器，分镜使用内建 Mock planner，视频仅由本地模拟 worker 上传合成 MP4 容器。定价相关测试只写临时 JSON，真实模型适配器替换为禁止调用的 Mock。测试不读取真实 `data/`，不调用图片、文本或视频供应商，不启动真实视频执行器。

| 测试 | 验收行为 |
| --- | --- |
| 01 | 未启用分镜时保留原脚本生成、TXT 下载流程 |
| 02 | 脚本完成后服务端启动分镜，绑定原 Artifact 和指定脚本版本 |
| 03–04 | 脚本失败不启动分镜；分镜失败保留已完成脚本和 TXT |
| 05–06 | 历史脚本版本明确选择；剧情时长和生成时长区分，冲突显式报告 |
| 07–11 | 更新、增删、拆分、合并、排序的稳定 ID、来源关系和旧版本保留 |
| 12–14 | 局部重规划先预览、确认后更新；旧版本拒绝覆盖；历史可读 |
| 15–16 | 图片 Artifact 缺失阻止；项目图片映射 Artifact 与 I2V 首帧 |
| 17–18 | 视频草稿不执行；精确绑定版本；模拟成功生成 MP4 Artifact；旧结果过期后保留 |
| 19–20 | 模型时长限制与审核/明确草稿试运行 |
| 21 | 默认关闭真实规划且不冻结积分 |
| 22–23 | 提交/刷新/执行重放不重复；账号、Workspace、CSRF 边界 |
| 24 | 原视频流程、脚本、参考图与每秒计费回归 |
| 25–26 | 分镜独立未定价和余额不足在模型执行、积分冻结前阻止 |
| 27 | 编辑请求幂等；复用 request_id 改参数必须拒绝 |
| 28 | 非连续局部重规划分别替换两端，中间剧情内容与顺序不变 |

`tests/test_storyboard_planner.py` 另有 6 项测试：独立模型及思考档位和提示词约束、一次调用、非 JSON/围栏/空输出拒绝与退款、结构验证、模型不支持的时长拒绝、Mock 毫秒分配和按剧情变化的镜头数、长对白时长冲突且保留正文。所有 Codex 分支均由 Mock 适配器提供 JSON。

`tests/test_storyboard_recovery.py` 有 23 项测试，覆盖独立费用冻结/消费/退款、无确认不执行、余额和价格变化、outbox 恢复、候选归档恢复、脚本完成到分镜入队的中断、并发重复提交、不明结果不重跑、过期候选不能覆盖、同项目未提供给模型的引用拒绝、原图片丢失时禁止扣费、项目移动后的权限、原 Artifact 身份、取消退款前中断补结算、关闭真实模式后只恢复已有凭据、JSON 临时文件枚举竞态和 Windows 文件共享句柄重试。

`tests/test_storyboard_followup.py` 增加 3 项后端回归：排队期间 Skill 变化不会改变实际执行的已保存提示词；仅修改标题不会清除模型报告的语义时长冲突；将目标及镜头时长调整到合理值后可明确审核通过。分镜后端专属合计 60 项，全部通过。

`tests/frontend/storyboard.test.js` 增加 11 项 Node VM 行为用例，由公共 `test_frontend_modules` 纳入完整检查。覆盖分镜间请求/候选隔离、放弃后新任务、服务端已收请求但响应丢失时复用幂等 ID、failed/cancelled 后明确重试、只修改画幅、参考图顺序、T2V/I2V 转换和缺失资产阻止草稿。它模拟 DOM 事件和 API 响应，不将模拟结果当作真实浏览器布局验收。

浏览器验证使用独立临时服务和已登录的临时测试账号。前端 Mock 场景 14 项通过；真实 HTTP 联调验证独立脚本页与视频工作台内两阶段生成，编辑/拆合/拖动、多选局部候选/接受及原视频草稿。最终 smoke 的脚本和分镜均为 completed，JS 错误与 HTTP 失败均为空；见 `work/storyboard-planning-20261020/ui-final-smoke.json`。测试没有发出视频 `/run` 请求；成功 MP4 的绑定和保留由自动化模拟 worker 验证。

本轮浏览器复核使用 `work/storyboard-followup-20261020/serve_qa.py`，绑定 127.0.0.1:8784 与全新临时 Storage。验证 v1→v2 只改画幅、v2→v3 保存首帧引用；两份分镜分别产生候选并能恢复；放弃后同样参数得到不同任务 ID；创建的原视频草稿绑定 v3 和原稳定 Shot ID，模式 I2V、画幅 9:16、4 秒、1 张参考图。余额仍为 200、冻结为 0，视频任务未执行。证据为同目录 `browser-bound-draft.png`、`browser-video-result.png`、`acceptance.json`。控制台警告/错误为空。

`test_video_pipeline` 和 `test_script_service` 继续验证原有流程。用例通过不代表真实模型质量、真实 H3 编码器或生产环境已验证。
