# GitHub 成熟能力候选评估

查询日期：2026-10-13（Asia/Shanghai）。用户随后选择 **A：看图缩放 + 基底/结果对比**，PhotoSwipe 和 img-comparison-slider 已获批准进入 R11。其余候选继续保留为研究参考。下文关于“未安装/未执行”的描述是研究交付时状态，实施与最终验收以 `work/iterations/R11/review.md` 为准。

建议优先考虑 **PhotoSwipe 图片查看、img-comparison-slider 编辑前后对比**；把 **restic 加密备份保管**作为独立可选项。前两项直接改善看图与审核，第三项补齐现有完整备份的加密保管能力。当前不建议替换任务队列、改存储格式或引入整套追踪平台。

本轮只读取项目与公开资料，唯一新增文件为本报告。未安装候选、未把第三方代码写入仓库、未新增依赖、未改迭代状态，未操作真实数据、`111` 账号、生图额度、远程存储或公开服务。以下接入和验收均为建议，尚未执行。

## 判断依据与项目接入边界

已读取 `README.md`、`docs/commercial_readiness.md`、`work/iteration-plan.md`、`requirements-web.txt`，并核对图片页面、账号分派、任务与备份源码。只读查询当前虚拟环境得到 FastAPI 0.143.0、Starlette 1.7.0、Uvicorn 0.54.0；这仅代表本机环境。

| 现有能力 | 可利用的接入点 | 必须保留的行为 |
| --- | --- | --- |
| 原生 JS + Jinja2；结果点击打开基础 `<dialog>` | `web_static/workbench.js` 的 `renderArtifact()`、`openLightbox()`；`web_templates/index.html` 的 `imageDialog` | 继续使用同源受保护的图片 URL；保持下载、审核、继续修改入口 |
| 图片 DTO 已有尺寸、`source_artifact_id`，任务详情含 `base_image` 与 `outputs` | `workbench_state.py` 的 `artifact_dto()` 和任务详情返回 | 比较仅涉及当前有权访问的图片；不新增图片、不变更历史版本与哈希 |
| 图片访问经账号/Workspace 校验，受控接口返回原图 | `web.py` 的 `/api/artifacts/{artifact_id}`，`accounts_web.py` 的分派 | 查看器不能把私有图改为公开静态文件，不能把 URL 或提示词发给第三方服务 |
| `AccountApplication.scoped_app()` 按账号和 Workspace 缓存子应用 | 未来监控应明确统一注册点与共享 registry | 不能给每个子应用重复注册同名指标，也不使用账号、任务、原始路径作为指标标签 |
| 顺序 Worker、显式提交、`uncertain` 和积分状态相互约束 | `worker.py`、`remote_queue.py`、`workbench_state.py` | 断线或结果不确定时不自动重放收费生成；归档恢复和重新生成继续区分 |
| 标准库完整备份、清单校验、SQLite backup、隔离恢复 | `scripts/backup_workbench.py` 与 [备份恢复说明](backup_restore.md) | SQLite、JSON、队列和图片需要业务一致性；restic 只保管已验证副本，不能解除停写要求 |

前端库不会解决服务端项目首次查询耗时；该问题仍应按性能实测独立处理。成熟性判断综合发布、维护提交、文档、接口与故障语义，未按星数排序。

## 8 项候选总览

“成本”是对本项目的接入判断，不是已经完成的工时或性能测量。GitHub 元数据本次查询显示八个仓库均未归档。发布与提交日期分开列出；最新提交可能仅修改文档或开发依赖，不代表最近修复了运行时问题。

| 候选 | 用户价值与接入方式 | 体积、依赖与迁移成本 | 本轮建议 |
| --- | --- | --- | --- |
| [PhotoSwipe](https://github.com/dimsemenov/PhotoSwipe) | 结果图缩放、拖拽、触摸查看；替换局部大图预览 | 无框架迁移；5.4.4 两个压缩 JS 与 CSS 合计约 73.8 KiB，未计 HTTP 压缩；无运行时 npm 依赖 | **首选：直接前端依赖候选** |
| [img-comparison-slider](https://github.com/sneas/img-comparison-slider) | 基底图与输出图拖动对比，帮助人工审核编辑是否符合要求 | 原生 Web Component；官方称压缩 JS 小于 12 kB、gzip 小于 4 kB，属上游口径；8.0.7 包未声明运行时依赖 | **首选：直接前端依赖候选** |
| [restic](https://github.com/restic/restic) | 对完整业务备份加密、去重、校验和恢复 | 独立可执行程序，不进入 Python 请求链；0.19.1 Windows amd64 ZIP 为 11,237,567 字节；需保管密码、仓库和恢复说明 | **第三推荐：可选运维工具** |
| [Cropper.js](https://github.com/fengyuanchen/cropperjs) | 上传前裁剪/旋转参考图 | 2.x 为 12+ 模块组成的组件体系，可按需使用；需新增草稿、导出、尺寸限制与“原图/裁剪图”语义，实际包体待构建测量 | 暂缓依赖；先借鉴裁剪交互 |
| [Prometheus FastAPI Instrumentator](https://github.com/trallnag/prometheus-fastapi-instrumentator) | 观察请求耗时、状态码、错误比例 | 8.1.0 依赖 Starlette 1.x 和 prometheus-client；无浏览器包体，需指标入口与采集方；动态子应用需要适配 | 借鉴指标设计，后续可选依赖 |
| [OpenTelemetry Python Contrib](https://github.com/open-telemetry/opentelemetry-python-contrib) | 串联 HTTP、远程传递与执行阶段，定位跨进程等待 | FastAPI 插件外还有 ASGI、API、语义约定等依赖；实际追踪需 SDK、exporter/采集端及脱敏策略 | 当前不引入整套；借鉴阶段关联 |
| [Huey](https://github.com/coleifer/huey) | 通用后台任务、调度、重试，支持 SQLite 后端 | 库本身较轻，SQLite 路径不必增加 Redis；迁移作业领取、恢复、积分一致性与远程协议成本高 | **当前不适合替换生成队列** |
| [Litestream](https://github.com/benbjohnson/litestream) | SQLite 持续复制与按时点恢复 | 独立 Go 进程、额外副本和配置；当前有 Windows 支持，但不备份图片/JSON | **当前不适合作为完整业务恢复方案** |

PhotoSwipe 字节数来自本次在内存中读取锁定版本的 [Core](https://raw.githubusercontent.com/dimsemenov/PhotoSwipe/v5.4.4/dist/photoswipe.esm.min.js)、[Lightbox](https://raw.githubusercontent.com/dimsemenov/PhotoSwipe/v5.4.4/dist/photoswipe-lightbox.esm.min.js)、[CSS](https://raw.githubusercontent.com/dimsemenov/PhotoSwipe/v5.4.4/dist/photoswipe.css)，分别为 54,270、14,338、7,003 字节；不是本站首屏实测。restic ZIP 大小来自 [官方 release API](https://api.github.com/repos/restic/restic/releases/tags/v0.19.1) 的资产元数据，未下载该二进制。其他候选未实测安装后体积，不能将仓库大小当作运行时成本。

## 发布、维护与许可证据

| 候选 | 查询到的相关版本 / 发布时间 | 默认分支最近提交 | 许可与采用时的实际义务 |
| --- | --- | --- | --- |
| PhotoSwipe | [v5.4.4，2024-05-24](https://github.com/dimsemenov/PhotoSwipe/releases/tag/v5.4.4) | [2025-12-04，文档更新](https://github.com/dimsemenov/PhotoSwipe/commit/cd41cb587a460634e4cae53134f3d01d06e284a6) | [MIT](https://raw.githubusercontent.com/dimsemenov/PhotoSwipe/v5.4.4/LICENSE)：分发代码/构建产物保留版权与许可文本 |
| img-comparison-slider | [8.0.7，2026-01-29](https://github.com/sneas/img-comparison-slider/releases/tag/img-comparison-slider-v8.0.7) | [2026-08-20，开发依赖更新](https://github.com/sneas/img-comparison-slider/commit/c73939d61c8fb010342c9910f1de420edbf0b067) | [MIT](https://raw.githubusercontent.com/sneas/img-comparison-slider/img-comparison-slider-v8.0.7/LICENSE)：同上；浏览器静态资源分发也保留声明 |
| restic | [v0.19.1，2026-07-05](https://github.com/restic/restic/releases/tag/v0.19.1) | [2026-10-11，forget 并发锁修复](https://github.com/restic/restic/commit/9da3b87b07706956e8dc5fd5cb259b99ec940f51) | [BSD-2-Clause](https://raw.githubusercontent.com/restic/restic/v0.19.1/LICENSE)：源码保留声明；若随产品分发二进制，在材料中保留版权、条件和免责文本 |
| Cropper.js | [v2.3.0，2026-10-07](https://github.com/fengyuanchen/cropperjs/releases/tag/v2.3.0) | [2026-10-08，动态 src 后零尺寸修复](https://github.com/fengyuanchen/cropperjs/commit/6b53510d9820e2c0956d63e1f8ad54c81f0351fe) | [MIT](https://raw.githubusercontent.com/fengyuanchen/cropperjs/v2.3.0/LICENSE)：保留版权与许可；各打包模块需一起核对 |
| Instrumentator | [v8.1.0，2026-07-26](https://github.com/trallnag/prometheus-fastapi-instrumentator/releases/tag/v8.1.0) | [2026-07-26，发布准备](https://github.com/trallnag/prometheus-fastapi-instrumentator/commit/2fca3e8dcbc11f520332d94181022ceb85a1b721) | [ISC](https://raw.githubusercontent.com/trallnag/prometheus-fastapi-instrumentator/v8.1.0/LICENSE)：保留版权和许可；README 还声明 routing 部分采用 BSD-3-Clause，采用前须核对发行包内第三方 notices |
| OpenTelemetry | FastAPI 插件 [0.66b1，2026-10-06](https://github.com/open-telemetry/opentelemetry-python-contrib/releases/tag/v0.66b1) | [2026-10-12，传播测试补充](https://github.com/open-telemetry/opentelemetry-python-contrib/commit/a953ae267b6429717092a150f3464e7d0378fb26) | [Apache-2.0](https://raw.githubusercontent.com/open-telemetry/opentelemetry-python-contrib/main/LICENSE)：附许可证、保留相关归属/NOTICE、修改文件说明变更；不包含商标授权 |
| Huey | [3.4.0，2026-09-04](https://github.com/coleifer/huey/releases/tag/3.4.0) | [2026-10-11，更新变更记录](https://github.com/coleifer/huey/commit/7e937f9d741f4fbc6fa8a18e589826f5f61554aa) | [MIT](https://raw.githubusercontent.com/coleifer/huey/3.4.0/LICENSE)：保留版权与许可 |
| Litestream | [v0.5.17，2026-08-31](https://github.com/benbjohnson/litestream/releases/tag/v0.5.17) | [2026-10-09，VFS 时间更新修复](https://github.com/benbjohnson/litestream/commit/fd72802bc885fb535152f9a899ab6ce274d47ed3) | [Apache-2.0](https://raw.githubusercontent.com/benbjohnson/litestream/v0.5.17/LICENSE)：保留许可/相关归属/NOTICE、标记修改 |

以上许可证允许在遵守条件下商业使用；这不能替代本项目上游 Codex 服务合同、用户图片权利或收费方案的核实。OpenTelemetry 仓库包含多个独立包，GitHub 的 latest release 当前指向 OpAMP 组件，本报告使用 FastAPI 插件对应的 0.66b1，未混用版本。

## 推荐一：PhotoSwipe，只增强结果图查看

最小范围：沿用现有结果卡片，把 `openLightbox(artifact)` 连接到固定版本的本地静态资源，增加缩放、拖拽、触摸操作、键盘关闭；首批仍只打开当前选中图片，保留原下载链接。Core 按需加载即可，无须引入 React、构建整套 npm 应用或改服务端数据模型。

适配依据：已有 DTO 的 `width`、`height`、`url` 满足其尺寸和数据源要求。[官方接入说明](https://photoswipe.com/getting-started/)支持原生 JS 和动态导入，但建议图片不超过 3000×3000。首批对更大尺寸沿用原图链接/现有预览；若需要高性能超大图，再单独评估受保护的缩略图服务，不在本次顺带引入实验性切片插件。

维护风险：稳定版发布时间较早，官方正在开发 v6；采用时锁定经验证的 v5，不追随开发分支。[安全页](https://github.com/dimsemenov/PhotoSwipe/security)本次返回无 SECURITY.md、无已发布公告，这不代表没有漏洞。只用文本节点显示用户标题，不启用任意 HTML caption；静态资源本地托管，不把私有图片交给上游站点。

验收：用合成 PNG/JPEG/WEBP 样本检查鼠标、键盘和触摸操作；关闭后焦点回到触发处；超大图有明确回退；失效会话/越权图片仍被原 API 拒绝；网络面板无新增第三方请求，打开查看器不产生 POST、不触发生成或积分变化；原图哈希和历史 ID 保持。

## 推荐二：img-comparison-slider，只比较基底与当前输出

最小范围：对带 `base_image` 的编辑任务提供“与基底对比”。优先仅对同尺寸、同画布的两张图片开放拖动叠加；尺寸或比例不同就显示并排查看和尺寸说明，避免强行拉伸导致错误判断。首批不做任意跨项目选图、差异评分、像素检测或自动审核。

直接利用详情中的基底和输出 DTO，不需要新建数据库关系。[官方示例与属性](https://github.com/sneas/img-comparison-slider)提供键盘控制、触摸和横/纵方向；采用本地固定版本 Web Component，不使用 README 示例中的浮动 CDN 大版本地址。8.0.7 的[发布记录](https://github.com/sneas/img-comparison-slider/releases/tag/img-comparison-slider-v8.0.7)专门修复过 CSP nonce 问题，仍需在本站真实 CSP 下验证。

维护风险：项目范围小、依赖少，但维护资源有限；[安全页](https://github.com/sneas/img-comparison-slider/security)返回无 SECURITY.md 和已发布公告，页面缓存不是实时漏洞保证。比较是视觉辅助，不能据此自动通过审核，也不能推断两张不同构图图片已经对齐。

验收：一张合成基底和仅局部改色的副本，可用鼠标和键盘准确切换；透明背景采用一致棋盘底；不同比例、图片加载失败与会话失效有回退；只请求已授权的两张图；拖动不修改图片或审核状态，不创建任务、不扣积分；页面切换销毁旧实例，避免旧 Workspace 图片残留。

## 推荐三：restic，保管已完成的业务备份

最小范围：用户选择本地或离线介质上的新备份仓库后，用独立脚本包装固定版本 restic，输入只接受现有备份工具已完成且验证通过的副本；执行加密保存、检查和恢复到全新隔离目录。首批不配置云桶、不购买存储、不自动删除快照、不安排服务自动切换。

这一项与现有工具分工明确：项目备份工具负责停写后的 SQLite/JSON/图片一致性和清单，restic 负责加密与增量保管。restic 支持 Windows、可重复构建和内容去重，见[官方仓库](https://github.com/restic/restic)；0.19.1 发布仍在修复备份路径、Windows SFTP 等行为，采用前要核对与所选存储方式有关的问题。

真实风险：密码丢失会使加密副本无法恢复；应先决定密码保管位置和恢复操作者。加密副本并不消除本地暂存明文副本，也不保证所有 Windows ACL 自动恢复。restic 支持 VSS，但[官方备份说明](https://restic.readthedocs.io/en/stable/040_backup.html)中的文件系统快照不等于本项目跨账本、队列和图片的业务事务，因此不凭 VSS 解除现有停写要求。安全公告页本次[未列公开公告](https://github.com/restic/restic/security/advisories)，不是完整依赖扫描。

验收：使用临时合成业务数据完成“现有 backup → verify → restic 保存 → restic 恢复 → 原工具 verify/restore”；核对账号归属、余额、账本、版本关系和图片哈希。需执行本地样本的 `check --read-data`，因为[默认 check 不读取全部数据包](https://restic.readthedocs.io/en/stable/045_working_with_repos.html)。错误密码、损坏仓库和恢复目标已存在必须失败；恢复目录权限受控；不启动 Worker、不重放队列。实际数据和远程备份留待单独明确对象与授权。

## 其余五项暂不进入首批的原因

**Cropper.js：需求成立后再引入。** 它能改善参考图构图，但会改变实际送入模型的素材；用户需要能区分原图、裁剪副本与取消编辑。还要覆盖导出尺寸、透明度、EXIF 方向、浏览器 Canvas 内存和服务端再校验。[2.x 官方说明](https://fengyuanchen.github.io/cropperjs/guide.html)显示其采用多组件体系，与旧 1.x 接口不同；2.3.0 发布后默认分支又修复了动态图片尺寸问题，不能把尚未发布的修复算进锁定版本。当前借鉴裁剪草稿与明确保存的交互即可，不扩展成图片编辑器。

**Instrumentator：保留为下一阶段的可选监控。** [8.1.0 元数据](https://raw.githubusercontent.com/trallnag/prometheus-fastapi-instrumentator/v8.1.0/pyproject.toml)要求 Python ≥3.10、Starlette ≥1,<2，静态版本范围覆盖本机 Starlette 1.7.0；这不是运行兼容性测试。现有动态子应用结构必须先明确统一指标注册、请求归一化和管理员采集入口。它默认覆盖 HTTP，不会自动知道 Codex 排队/执行时间；仍需业务埋点。优先借鉴“请求耗时、失败计数、队列深度、Worker 最后心跳”四类低基数指标。监控端点不可公开，无账号/任务/提示词标签；服务耗时、内存和重复注册须验收后再启用。README 的 ISC/BSD 声明与锁定发行包 notices 尚未完成逐文件审计。

**OpenTelemetry：等跨机器排障成为高频需求。** [FastAPI 插件](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html)有请求钩子、排除 URL 和请求头脱敏配置，适合以后关联云端与本机，但不会自动追踪当前 Codex 子进程的业务阶段。当前 FastAPI 插件为 beta 命名版本 0.66b1，需与其他 OTel 包协调升级。上游存在历史[指标基数 DoS 公告](https://github.com/open-telemetry/opentelemetry-python-contrib/security/advisories/GHSA-5rv5-6h4r-h22v)；2026 年另有[可选 OT Trace propagator 公告](https://github.com/open-telemetry/opentelemetry-python-contrib/security/advisories/GHSA-xvh2-qr4p-hfvp)，后者影响 ≤0.65b0、0.66b0 修复且默认未启用，不能误称当前 FastAPI 插件必然受影响。当前不增加 collector 或第三方遥测上传。

**Huey：借鉴故障语义，不迁移生成任务。** [官方指南](https://huey.readthedocs.io/en/latest/guide.html)明确任务按 at-most-once 交付，突然终止时正在执行的任务可能丢失；其重试也不替代业务幂等。当前 `uncertain`、执行 ID、冻结积分与断线回传已经形成一套恢复规则，接入通用队列仍得重建这些约束，并不自动获得“恰好一次生图”。未来若出现与付费生成无关的缩略图/报告后台工作，可重新评估独立队列；当前没有必要引入第二套任务系统。

**Litestream：不能只恢复账本而遗漏图片与队列。** 已核实官方提供[Windows 运行说明](https://litestream.io/guides/windows/)，不能用“不支持 Windows”排除；但其[目录复制](https://litestream.io/guides/directory/)复制的是多份 SQLite 数据库，不是通用文件夹。恢复较新的积分库搭配较旧的项目 JSON/图片，仍会破坏本项目一致性。另[0.5 迁移说明](https://litestream.io/docs/migration/)有配置/格式变化、保留策略迁移和 Age 加密不可用等边界，不能拿旧版加密能力作现版承诺。只有将来明确数据库级恢复目标并解决跨文件对齐后，再考虑作为补充复制层。

## 供用户选择的最小方案

| 选项 | 进入下一轮的范围 | 需要决定的内容 |
| --- | --- | --- |
| A：先做看图与对比（推荐） | PhotoSwipe 当前单图查看 + 同画布的基底/结果对比；固定版本、本地静态资源、保留旧预览回退 | 是否将这两项加入功能线 |
| B：只做版本对比 | 只引入 img-comparison-slider；现有大图预览保留 | 若最看重审核编辑差异，可选此更小范围 |
| C：另开本地加密备份验证 | restic 仅操作临时样本和已验证副本，先验证恢复 | 是否要此能力；真实启用前明确存储位置、密码保管人和恢复目标 |
| D：暂缓引入 | 保存本报告，先完成当前性能与恢复工作 | 无新增依赖或功能承诺 |

研究交付时尚未批准任何选项；用户看过后已明确选择 A，并建立 R11 实施项。未选择的候选继续保留为参考。采用直接依赖时固定版本/来源摘要，保留 LICENSE 和必要第三方声明，测试失败可移除接入层并回到旧行为，业务数据无需迁移。

## 已核实与尚未核实

已核实：项目实际接入点；八个官方仓库及许可证正文；相关发布版本和日期；默认分支最近提交；首选组件的依赖元数据与主要限制；restic Windows 发布资产信息；部分上游安全公告。资料优先使用官方仓库、项目文档、GitHub API 与官方包元数据，引用均指向直接来源。

尚未核实：候选在本站的浏览器/CSP/可访问性表现、与当前认证/缓存的联动、完整供应链漏洞扫描、安装后资源占用、端到端恢复耗时，以及任何真实云端、HTTPS、成本或商用合同条件。未安装和集成，因此不能称为已兼容、已安全或可直接上线。GitHub 网页可能带缓存；匿名 API 在末尾触发限流，后续源码路径确认改用公开网页与 raw 文件，未使用账号凭据规避限制。正式采用前应按锁定版本复核当日发布与公告。
