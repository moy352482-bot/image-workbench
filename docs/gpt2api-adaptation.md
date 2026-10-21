# gpt2api 设计参考与接入边界

核对日期：2026-10-13。参考基线固定为 [laowang74152/gpt2api 的提交 `bebe75062f0be07dec223f66d733df50776ee0e6`](https://github.com/laowang74152/gpt2api/commit/bebe75062f0be07dec223f66d733df50776ee0e6)（2026-04-18），以下为静态审查结论，不代表其线上能力已验证。本项目仅借鉴通用设计并独立实现，未运行该仓库、导入真实账号凭证或调用其网页生图链路。

## 许可与源码实际行为

[LICENSE 原文](https://raw.githubusercontent.com/laowang74152/gpt2api/bebe75062f0be07dec223f66d733df50776ee0e6/LICENSE) 是 **MIT 正文＋附加研究用途声明**。MIT 正文允许使用、复制、修改、分发、再许可和销售，要求复制件或实质部分保留版权及许可通知，并声明按原样提供。后附声明包含 “for educational and research purposes only”，并说明使用者须遵守相关服务条款。因此不能只标注“标准 MIT，可无条件商用”；直接复制用于商业产品时，附加措辞的授权边界需另行澄清。本次不复制其实现代码。

| 设计 | 源码证据与限制 |
| --- | --- |
| 资源独占 lease | [Redis 锁](https://raw.githubusercontent.com/laowang74152/gpt2api/bebe75062f0be07dec223f66d733df50776ee0e6/pkg/lock/redis_lock.go) 使用 `SET NX`，释放时校验持有者 token。虽有 `Refresh` 方法，审查范围内未见调用。[调度器](https://github.com/laowang74152/gpt2api/blob/bebe75062f0be07dec223f66d733df50776ee0e6/internal/scheduler/scheduler.go#L79) 的未配置回退 TTL 为 180 秒；[示例配置](https://raw.githubusercontent.com/laowang74152/gpt2api/bebe75062f0be07dec223f66d733df50776ee0e6/configs/config.example.yaml) 是 1200 秒。[Runner](https://github.com/laowang74152/gpt2api/blob/bebe75062f0be07dec223f66d733df50776ee0e6/internal/image/runner.go#L74) 默认单次超时 300 秒，因此回退配置存在锁先过期风险，不能说示例配置必然发生同样问题。 |
| 排队与异步 | 调度器通过有截止时间的退避轮询等候资源，并非持久化 FIFO 队列。[README](https://github.com/laowang74152/gpt2api/blob/bebe75062f0be07dec223f66d733df50776ee0e6/README.md) 宣称 `async:true` 可立即返回任务 ID；实际 [ImageGenRequest 与 handler](https://github.com/laowang74152/gpt2api/blob/bebe75062f0be07dec223f66d733df50776ee0e6/internal/gateway/images.go#L61) 没有该字段，生成路径同步执行，文档所述异步提交未在此实现。 |
| 任务状态 | [任务模型](https://raw.githubusercontent.com/laowang74152/gpt2api/bebe75062f0be07dec223f66d733df50776ee0e6/internal/image/model.go) 保存状态、时间、结果和错误码，但 Runner 在拿到 lease 前便标为 running；终态写入复用请求 context 且忽略写库错误，连接取消或超时可能留下未收尾任务。 |
| 冷却恢复 | [MarkRateLimited](https://github.com/laowang74152/gpt2api/blob/bebe75062f0be07dec223f66d733df50776ee0e6/internal/scheduler/scheduler.go#L317) 写入 throttled 和冷却时间；[候选查询](https://github.com/laowang74152/gpt2api/blob/bebe75062f0be07dec223f66d733df50776ee0e6/internal/account/dao.go#L109) 只选择 healthy。`RestoreHealthy` 在本次检查的业务代码中未见调用，不能把冷却到期自动恢复当作已验证能力。 |
| 后端分层 | handler、Runner、DAO 有职责划分，但 Runner 直接构造具体 ChatGPT client，不能据此声称已有通用可替换后端。README 的高并发或模型能力宣称也不等于经过本项目验证。 |

## 本网站的适配范围

本网站已有持久化异步 job、远程领取 `claim_id`、执行 `attempt_id`、积分冻结与幂等结算，以及 `ImageBackend` 扩展接口。依据为 [远程队列](../image_workbench/remote_queue.py)、[后端接口](../image_workbench/image_backend.py) 和 [积分契约](accounts_workspaces_credits.md)。这些能力应继续复用。

适合当前单机队列的三项设计是：

1. **资源占用与容量控制。** 在现有远程网关领取流程增加全局及每网站账号的并发上限，两项默认均为 1；`local` 仍串行。领取者身份及执行尝试继续由现有标识约束，无须为这次修改引入 Redis 或 MySQL。
2. **持久状态与明确收尾。** 保留已有异步任务和冻结结算契约。`uncertain` 表示执行结果尚不明确，继续占用容量，不因等待超时自动释放、退款或重复提交。提供管理员只读状态视图，用于判断任务占用和调度情况。
3. **统一后端接口。** 生图后端接收统一请求并返回本次产物，任务归档、权限、计费及状态仍由网站负责；未来支持的生图服务可经 `ImageBackend` 接入。

这里的 `account_id` 指网站账号及其业务归属，**不是上游 ChatGPT 账号**。上述并发控制不建立上游账号池，也不证明多个 Worker 绑定了独立上游账号或拥有更多生图额度。本次范围不包含上游账号池、自动换号、冷却重试或网页协议接入。

## 网页／桌面账号生图的接入判断

从网站架构看，可以保持同一个网站端口和现有任务接口，由服务器将任务交给内部生图后端；端口不是主要障碍。限制在于上游是否提供受支持的调用及授权方式。参考仓库使用私有网页协议，并包含规避站点保护机制的实现，不能把它等同于受支持的账号生图接口；本项目不引入这些实现。

截至核对日期，官方 [Sign in with ChatGPT 预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) 明确将 image generation 列为该路由不支持的工具。允许 image input 只表示可输入图片，不等于允许生成图片，也不能据此推断网页或桌面订阅可直接作为网站生图 API。

可选方向是继续使用现有 Codex 链路；将来若出现正式支持的账号生图授权，再核实能力与许可后接入；或在用户明确同意独立 API 费用后接入正式图片 API。[官方图片生成指南](https://developers.openai.com/api/docs/guides/image-generation) 说明 Image API 与 Responses API 的生成能力；[官方计费说明](https://learn.chatgpt.com/docs/pricing) 区分 API 计费与订阅使用量，不能把订阅额度当作已包含 API 调用费用。

本次没有接通或调用 ChatGPT 网页／桌面账号生图，没有新增付费 API 调用，也未对真实账号进行接入验证。
