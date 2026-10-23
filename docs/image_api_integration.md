# 图片 API 接入

2026-10-23，依据用户提供的《API接口文档.md》v1.0 添加 `image-api` 图片 Provider。
用户补充供应方入口后，确认实际基础地址为 `https://subdirect.aicodexvip.top`，
使用 `gpt-image-2`。最终版本已完成三轮真实生成和编辑验收，6 次请求均成功。
完整发布回归和线上切换结果见 [本次验收记录](image_api_release_20261023.md)。

## 接入范围

- `ImageAPIBackend` 属于 Provider，放在 `architecture.json` 的 `image_backend` 分组。
  仅依赖既有 `BackendRequest`、`BackendResult`、`BackendError` 和 HTTP 客户端。
- 复用 `TaskRunner`、`LocalWorker`/`RemoteWorker`、`WorkbenchState`、`StorageManager` 和
  `IdentityStore`，不新增任务队列、数据模型或 Core 事务。HTTP/CLI 只增加后端选项。
- 无参考图调用 `/v1/images/generations`；带参考图生成、已有图编辑调用 `/v1/images/edits`。
  编辑基底排在附加参考图之前，以 `images: [{image_url: "data:image/png;base64,..."}]`
  发送（MIME 类型随图片变化）；每个任务请求 1 张图片。文档的独立 `base64` 字段实测返回
  HTTP 400，已按供应方实际支持的 Data URL 修正，没有自动改参数重复计费。
  只请求 `b64_json` 返回，候选图写入本次 `products/`，由既有流程检查和归档，不接收任意下载地址。
- `1:1`、`3:2`、`2:3` 分别传 `1024x1024`、`1536x1024`、`1024x1536`；自动比例不发送 `size`。
  比例由原任务选项显式传至 Provider，远端清单增加可选 `aspect_ratio`，缺省仍为 `auto`。
  保留线上默认图片创作 Skill 和八种风格，比例不依赖解析编译后的提示词。
  参考图、编辑基底和历史版本沿用原有空间权限、快照、归档和来源关系。
- 网站积分延续原规则：纯文生图 40，带参考图或编辑 60，由原账本冻结、结算和退款。
  供应方实际收费由其 API 账号承担；未从文档推定价格。
- 不自动重试真实生成，不自动回退到 Codex；超时可能已经被上游处理，保存诊断后由用户决定重试。
  队列重启和传输重试仍使用原幂等检查点，不重新调用模型。归档恢复仍只处理原产物。
- 旧任务与旧磁盘格式可继续读取，`codex`、`local` 保留，默认仍是 `codex`。
  新增 `image_api_failed`、`image_api_timeout` 错误类型，远端回传按现有协议识别。

## 私密配置

将 `deploy/image-api.example.json` 复制为被 Git 忽略的 `deploy/image-api.local.json`，填写真实
`base_url`、`api_key`、`model`，然后在执行器进程环境中设置：

```powershell
$env:IMAGE_WORKBENCH_IMAGE_API_CONFIG = (Resolve-Path 'deploy/image-api.local.json').Path
```

也支持环境变量 `IMAGE_WORKBENCH_IMAGE_API_BASE_URL`、`IMAGE_WORKBENCH_IMAGE_API_KEY`、
`IMAGE_WORKBENCH_IMAGE_API_MODEL` 覆盖文件值；默认模型 `gpt-image-2`。支持站点根地址及 `/v1`
基础地址；远程地址要求 HTTPS。Key 不进入源码、网页配置、命令行参数或任务诊断。

本地网页/Worker/CLI 显式加 `--backend image-api`。`run`/`batch` 仍必须显式加
`--allow-image-generation`。云端服务可通过 `IMAGE_WORKBENCH_IMAGE_BACKEND=image-api` 选择新后端，
配套 `remote-worker --backend image-api` 运行在受控执行主机；Key 只交给该执行器。
服务器与 Worker 必须使用相同后端，各后端使用独立远程缓存。

服务器部署模板为 `deploy/image-api-worker.service`，用现有远程队列协议运行独立执行器。
Key 存放在 `/etc/image-workbench-image-api.json`，权限 `0600`、属主 `image-workbench`；
缓存使用 `/var/lib/image-workbench-image-api`，不覆盖旧执行器缓存。主服务重启会联动重启
API Worker；发布前仍须确认图片队列空闲。原 Codex 配置与程序保留。

切换前须确认旧后端没有排队、执行中或待确认作业，保留旧配置与旧 Worker 缓存。
回退时恢复 `codex` 配置和原执行器，无需迁移历史图片。回退程序代码时保留新任务及候选产物证据。

## 验证

针对性用例 `tests/test_image_api_backend.py` 使用临时目录和 `httpx.MockTransport`，覆盖生成、
多图参考、编辑版本关系、图像校验、错误脱敏、超时、禁止自动重试、CLI 授权开关、工作台 HTTP
提交到远程队列/回传/下载、积分结算与退款、失败任务重启不重放。旧 Codex 链路由图片模块原有
回归继续覆盖。检查入口：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/verify.py --module images
```

初版图片模块检查 142 项通过。合入线上现有风格和创作规则后，针对性检查 43 项通过，
含 13 项 API 专项用例；Ruff 无新增诊断，架构检查 87 个模块、425 条导入、0 违规。
发布仍执行完整统一检查，记录在本次发布包的 `checks.txt`，不以局部检查替代。

三轮实际验收使用独立临时数据和真实 API，经工作台 HTTP 提交、Worker、回传、归档、下载全链路：

| 轮次 | 生成 | 修改与检查 |
| --- | --- | --- |
| 1 | 1:1 红色陶瓷杯产品图 | 改为蓝色，检查杯型、构图、光照保留 |
| 2 | 3:2 湖边木屋场景 | 改为傍晚，检查建筑、湖岸位置保留与光线改变 |
| 3 | 使用前两轮图片作多图参考，生成产品场景 | 修改杯子颜色，检查主体与背景连续性 |

每轮独立发起一次生成、一次编辑，不用自动重试掩盖失败，不把 Mock 结果当作真实 API 性能。
每轮记录见 `work/image-api-integration/live-release-round-{1,2,3}/run.json`。
三轮结果通过后运行发布所需完整回归并上线；上线后核验实际版本、后端选择和执行器心跳。
