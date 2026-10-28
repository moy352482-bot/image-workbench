# DeepSeek Flash 与 SD2.0 接入

本变更将新网站的剧本、脚本、修改版本、AI 分镜和视频提示词调用统一到官方 `deepseek-flash`。剧本与分镜继续使用原有内置 Skill、模板、风格、案例及其冻结快照。视频入口只开放 SD2.0（内部 backend 保持 `online-2.0`），提示词由新的 `seedance-2-video` Skill 整理。

文字功能按用户决定暂为 0 积分，仍记录原有零金额预约、结算和失败结果。视频每秒积分、账号/Workspace 权限、历史版本及存储格式不改变。当前视频供应通道仅支持 720p 文生视频、5/10/15 秒；官方模型支持的其他输入不自动成为本站能力。

## 实现边界

- Provider：`deepseek_text.py` 负责官方 HTTPS API、完整结果检查与持久回执，不处理账号、账本或产物登记。
- Application Service：复用 ScriptService、StoryboardService、ScriptRemoteQueue、OnlineVideoPlanner 和 VideoStore；由 composition 注入适配器与 API 模型目录。
- Skill：原有脚本/分镜规则只调整模型适配器说明及视频示例；SD2.0 规则为独立编写，来源见 `skills/seedance-2-video/SOURCES.md`，未复制无明确再分发许可的官方 Skill 文件。
- Frontend：从现有模型配置读取文字模型和思考强度；只给新任务显示可用的视频模型。H3 历史仍可读、下载及按原后端恢复。
- 数据兼容：不迁移旧记录。API 回执新增 provider、模型、提示词/正文摘要、供应商请求号；不含密钥。旧 `codex/` 日志目录保留。新分镜 `mode=api`，旧 `codex` 值保留解析；新视频标识沿用 online-2.0。
- 共享 Core 不增加业务特例；新增 Provider 在 architecture.json 中登记为底层依赖，调用方只通过注入的文本端口使用它。

同步此分支时已保留网站现有图片 API、四任务并发、图片操作和 H3 引用修复，并纳入尚未提交的在线视频执行实现；其他工作区保持不变。

## 部署组成

发布包包含 `config/deepseek/*.json`、既有创作 Skill、新的 SD2.0 Skill 和 Python 模块。真实密钥单独保存在 `/etc/image-workbench-deepseek.json`，格式参考 `deploy/deepseek.example.json`，仅运行服务的用户可读。也支持 `DEEPSEEK_API_KEY` 环境变量；不向浏览器下发。

Web 服务需要 `IMAGE_WORKBENCH_TEXT_API_CONFIG=/etc/image-workbench-deepseek.json`，用于 AI 分镜和本地运行模式。云端脚本队列运行 `deploy/text-api-worker.service`；视频提示词队列运行 `deploy/video-prompt-worker.service`；视频生成继续运行现有在线 Worker。两个新增服务使用单独的持久缓存目录，避免与旧本地执行器共用日志。

`/etc/image-workbench-text-worker.json` 包含网站 HTTPS 地址、与服务器一致的 worker_token、独立 cache_root，格式见 `deploy/text-worker.example.json`。配置文件不在发布包内。安装 unit 后检查三类 Worker 的心跳和实际任务回执；仅页面可访问不足以证明 AI 路径已启用。

## 验证与回滚

`tests/test_deepseek_text.py` 验证完整文本、密钥不入回执、错误脱敏、超时不重放、回执篡改与固定路由。`tests/test_api_creative_flow.py` 验证脚本→分镜→SD2.0 提示词→视频提交，原 Skill 保留、文字零积分、视频预约和失败退款，以及云端 Worker 不依赖 Codex。

浏览器与历史协议回归继续覆盖旧 H3 队列、旧模型任务恢复、Workspace 隔离和晚到响应。旧兼容用例显式启用旧后端；生产 composition 只允许 SD2.0 新视频任务。接口枚举扩展必须保留全部旧值；没有自动刷新 Ruff 历史基线。

发布前运行 `.venv/Scripts/python.exe -X utf8 scripts/verify.py`。真实 DeepSeek 探针仅发送原创短示例，不使用业务数据；它不调用视频供应商。生产发布需单独确认当前运行版本和环境。

回滚先停止接收新 API 任务，完成或核对在途任务，再停新增 Worker 和回退 Web 版本；保留文本回执、视频 journal 与全部业务数据。不得把状态未知任务重新投给另一 Provider，也不得删除检查点来强行重跑。
