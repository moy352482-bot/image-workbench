# 图片 API 接入

2026-10-23，依据用户提供的《API接口文档.md》v1.0 添加 `image-api` 图片 Provider。
本地已实现，尚未调用真实 API、未发布或切换线上后端。提供的文档把服务地址写成
`https://your-domain.com`；附件也没有可提取的实际域名或下载来源。API Key 无法推导服务地址，
因此目前不能验证 Key、接口可达性、真实耗时、稳定性或编辑质量。

## 接入范围

- `ImageAPIBackend` 属于 Provider，放在 `architecture.json` 的 `image_backend` 分组。
  仅依赖既有 `BackendRequest`、`BackendResult`、`BackendError` 和 HTTP 客户端。
- 复用 `TaskRunner`、`LocalWorker`/`RemoteWorker`、`WorkbenchState`、`StorageManager` 和
  `IdentityStore`，不新增任务队列、数据模型或 Core 事务。HTTP/CLI 只增加后端选项。
- 无参考图调用 `/v1/images/generations`；带参考图生成、已有图编辑调用 `/v1/images/edits`。
  编辑基底排在附加参考图之前，以文档支持的 Base64 JSON 发送；每个任务请求 1 张图片。
  只请求 `b64_json` 返回，候选图写入本次 `products/`，由既有流程检查和归档，不接收任意下载地址。
- `1:1`、`3:2`、`2:3` 分别传 `1024x1024`、`1536x1024`、`1024x1536`；自动比例不发送 `size`。
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

本次结果：142 项全部通过（78.687 秒），含 13 项新 API 专项用例；Ruff 无新增诊断，
架构检查 86 个模块、422 条导入、0 违规。检查期间源码指纹未变化。日志保留在
`work/image-api-integration/module-checks.txt`。发布文件清单包含新 Provider，排除私密配置。
这些是离线功能回归，不能替代下述真实供应方测试。

得到真实地址后执行三轮实际验收，每轮均记录 API 耗时、工作台总耗时、状态、输出图片和修改效果：

| 轮次 | 生成 | 修改与检查 |
| --- | --- | --- |
| 1 | 1:1 红色陶瓷杯产品图 | 改为蓝色，检查杯型、构图、光照保留 |
| 2 | 3:2 湖边木屋场景 | 改为傍晚，检查建筑、湖岸位置保留与光线改变 |
| 3 | 使用前两轮图片作多图参考，生成产品场景 | 修改杯子颜色，检查主体与背景连续性 |

每轮独立发起一次生成、一次编辑，不用自动重试掩盖失败，不把 Mock 结果当作真实 API 性能。
三轮结果通过后才运行发布所需完整回归并上线；上线后核验实际版本、后端选择和执行器心跳。
