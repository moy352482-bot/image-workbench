# 本地验收记录

主目录完整检查通过：**共 639 项：635 项通过、4 项平台条件跳过，398.923 秒，退出码 0**。源码指纹在检查前后保持一致；架构、HTTP 契约、前端组件和后端回归均包含在内。完整原始日志：`work/asset-ux-20261020/verify-full.log`；摘要：[verification-summary.json](verification-summary.json)。

命令：`.venv/Scripts/python.exe -X utf8 scripts/verify.py`。

## 浏览器验证

真实 Chromium + uvicorn + 临时 SQLite/文件；只替换模型适配器为 Mock。35 项用例全部通过，未捕获页面异常 0、真实模型调用 0。图片 Worker/TaskRunner、上传与引用、积分事务、Artifact 和 Version 使用真实应用路径。

| 范围 | 用例 | 实测总时长 | 证据 |
| --- | ---: | ---: | --- |
| A：图片输入 | 8 | 23.569 秒 | [browser-A.json](browser-A.json) |
| B：暂存区与跨模块 | 7 | 22.760 秒 | [browser-B.json](browser-B.json) |
| C：脚本直接生成 | 13 | 74.670 秒 | [browser-C.json](browser-C.json) |
| D：图片生成闭环 | 3 | 13.452 秒 | [browser-D.json](browser-D.json) |
| D：分镜与脚本菜单 | 4 | 16.656 秒 | [browser-storyboard.json](browser-storyboard.json) |

闭环一：paste → 唯一 Artifact → 当前任务引用 → LocalWorker/TaskRunner 执行 Mock 图片生成 → 结果显示 → 继续修改 → 旧产物不变且来源版本正确。

闭环二：需求 → 模板和风格 → 模型与强度 → 自动积分 → 单击开始生成 → 真实额度校验 → Mock TXT → Artifact/Version → 手动修改 v2 → 刷新查看旧版本。

异常覆盖：非法/损坏图、上传与提交断网、权限及项目隔离、素材缺失、余额不足、模型不可用、重复点击、免费任务限流、失败退款、过期报价、旧异步响应、首帧保护、首次任务详情 503 自动恢复。未因恢复重放模型。

## 性能实测

使用本机真实 HTTP、24×16 PNG fixture。预览出现 8.30–9.50 毫秒；登记入库并在暂存区可见 283.90–311.50 毫秒；暂存区点击设为参考图 182.58–185.56 毫秒。该样本衡量交互开销，不代表大图或生产网络吞吐。

模型/思考强度的本地价格切换：10.12–15.10 毫秒。C 的 JSON 记录每次操作及等待可见结果的耗时；分镜 JSON 记录粘贴、拖拽保存、Mock 分镜和私有模板操作。价格切换和素材引用均不调用模型。

## 关键修复与回归

- 统一输入先验证整批文件、按子项目内容去重；真实格式与总量、数量在后端执行。
- 素材库任务禁止通用移动，上传再次复核归属；新增 2 项边界回归通过。
- 视频采用真实 MP4 Artifact 和同卷硬链接；验证同文件身份、旧 URL、绑定与移出后原文件保留。
- 脚本提交无需报价签名，服务端仍按当前 Registry 计算；未知价格字段被拒绝，免费调用有事务内次数/并发限制。
- Windows 归档与读取短暂竞争使用有界原子替换重试；5 项定向行为检查及 storage 回归通过。
- 任务首次详情读取失败后自动继续查询，错误原因和退款状态能恢复显示。

## 平台跳过

- test_rejects_outside_duplicate_and_linked_cache_paths (test_codex_backend.CodexBackendTests.test_rejects_outside_duplicate_and_linked_cache_paths) ... skipped "当前平台/权限不能创建文件符号链接: [WinError 1314] 客户端没有所需的特权。: 'D:\\\\ChatGPT\\\\图片网站\\\\tests\\\\tmpu1bsdb0h\\\\codex-home\\\\generated_images\\\\fresh.png' -> 'D:\\\\ChatGPT\\\\图片网站\\\\tests\\\\tmpu1bsdb0h\\\\codex-home\\\\generated_images\\\\linked.png'"
- test_failed_health_rolls_back_and_reports_failure (test_deploy_release.LinuxReleaseTests.test_failed_health_rolls_back_and_reports_failure) ... skipped 'Linux Bash 发布模拟在 WSL 单独执行'
- test_success_switches_release_without_changing_data (test_deploy_release.LinuxReleaseTests.test_success_switches_release_without_changing_data) ... skipped 'Linux Bash 发布模拟在 WSL 单独执行'
- test_candidate_and_codex_symlinks_cannot_read_or_write_outside_storage (test_script_recovery.ScriptRecoveryTests.test_candidate_and_codex_symlinks_cannot_read_or_write_outside_storage) ... skipped '当前平台不支持测试符号链接：OSError'

真实 Windows/macOS 剪贴板图片来源与 macOS Cmd+V 仍需人工设备验收。浏览器测试注入原生 paste/DataTransfer 事件，文本 Ctrl+V 经过真实浏览器键盘路径。未验证付费生产模型、线上部署或生产大素材性能。
