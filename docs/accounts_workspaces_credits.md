# 账号、工作空间与积分

## 使用方式

管理员用户名为 `moy`，额度显示“无限”；已有项目归入它的默认空间，历史任务不追扣。本人首次设置密码后登录。普通注册账号获得 200 积分及独立工作空间。

侧栏支持新建和切换 Workspace。项目 `⋯` 菜单支持重命名、编辑描述、归档、移动到本人其他 Workspace、删除。删除进入可恢复的回收站，保留图片和历史；归档项目从“查看已归档项目”进入。排队、执行中或结果待确认的项目不能移动或删除。本阶段不提供跨账号转移、团队权限或永久删除。

保存草稿不扣分，开始后先冻结，成功扣除，明确失败退回。

| 操作 | 每张积分 |
| --- | ---: |
| 纯提示词生成 | 40 |
| 参考图生成 | 60 |
| 历史图编辑 | 60 |

价格由服务端 `CreditCostCalculator(operation, model, image_count, reference_count)` 计算。当前 Codex 契约每次生成一张图片，配置了 `codex` 与演练用 `local` 策略；未知模型拒绝执行。参考图及基底从服务器记录读取，客户端不能指定价格、余额或账号归属。`local` 使用独立演练积分，不调用模型。归档失败退款后，再次恢复归档会重新冻结，恢复成功才扣除，不调用 Codex。

## 数据模型和事实源

| 数据 | 唯一保存位置 |
| --- | --- |
| Account | `.identity/accounts.sqlite3` 的 `accounts`：ID、用户名、可选邮箱、密码哈希、状态、时间、管理员标记 |
| Workspace | `workspaces`：空间 ID、账号 ID、名称、时间、存储根映射 |
| 主项目的空间归属 | `project_ownership`：主项目 ID → Workspace ID，及软删除标记 |
| 主项目、子项目、任务组织关系 | 原 `.project_management/catalog.json` |
| 任务、图片、文件、版本来源 | 原 StorageManager 的稳定物理目录及元数据 |
| CreditAccount | `credit_accounts`：总额、已用、冻结、更新时间、无限额标记 |
| CreditTransaction | `credit_transactions`：流水 ID、账号、空间、任务、作业、类型、数量、时间 |
| 冻结及待发布作业 | `reservations`：冻结状态与队列载荷（outbox） |
| 登录环境 | `sessions`：会话哈希、账号、当前空间、CSRF、过期时间、User-Agent |

可用积分=总额−已用−冻结。无限额通过 `is_unlimited=true` 表示，API 总额及可用额为 `null`，不使用非标准 JSON Infinity；仍记录管理员新任务流水。

设备只记录登录环境，不决定数据归属。Workspace 是逻辑隔离边界，当前 `storage_root='.'` 映射到同一个受管存储根；唯一归属表决定可见项目。图片仍位于 `projects/<稳定项目ID>/tasks/<稳定任务ID>/…`，跨 Workspace 移动只更新归属记录，文件和 ID 均不变。主项目 DTO 的 `workspace_id` 由该表投影，catalog 不保存重复空间关系。

HTTP 经 `WorkspaceProjects` 和 `WorkspaceStorage` 验证所属账号和空间后才读取资源。包括 `moy` 在内，所有账号的数据接口只允许本人空间。项目、任务、图片、下载、审核、运行及积分均受保护；前端不接收真实路径。空间通过会话或 `X-Workspace-ID` 选择，图片 URL 携带空间 ID，仍需账号校验。客户端不能通过指定项目 ID 认领未分配旧项目。

## 事务及任务状态

冻结、流水、outbox 在一个 SQLite `BEGIN IMMEDIATE` 事务中提交。余额约束防止超用；唯一约束防止同任务多个活动预约、同请求重复扣分及同作业重复结算。JSON 队列在冻结之后发布；发布中断时恢复同一作业 ID，完成记录已写入但结算中断时补做幂等结算。

本地 Worker、远程领取和启动都验证冻结记录。启用账号后，绕过队列的 `TaskRunner.run` 也不能启动未冻结请求。原执行锁、幂等执行 ID、Codex 后端和生成协议保留。远程 Worker 使用独立 Bearer 令牌，不能凭此令牌访问浏览器数据接口。

原界面继续展示草稿、待审核、已通过等业务状态；任务接口另有：

| 情况 | execution_status | credit_status |
| --- | --- | --- |
| 未提交 | waiting_credit | waiting_credit |
| 不足 | credit_insufficient | credit_insufficient |
| 冻结排队 | credit_frozen | credit_frozen |
| 开始或结果未知 | running | running |
| 成功 | completed | completed |
| 明确失败 | failed | refunded |

不足返回 HTTP 402 和预计/剩余积分，不创建执行作业、不启动模型。中断结果未知时保留冻结，显示“状态待确认”；通过既有人工确认停止流程确认失败后退款。恢复归档失败也退款，显式重试才重新冻结。

任务 DTO 的 `estimated_credit` 是按当前策略计算的下一次执行报价；已有预约另返回 `reserved_credit`，为账本保存的本次实际冻结/扣除/退款金额。后续政策改变不会改写已冻结或历史结算金额，界面分别展示本次金额与可能不同的重试报价。

## 登录

密码仅保存独立随机盐的 PBKDF2-HMAC-SHA256 哈希（600,000 次）。会话令牌随机生成，数据库只存哈希；Cookie 设置 HttpOnly、SameSite=Strict，HTTPS 增加 Secure。注销撤销会话，disabled 账号不能使用原会话。写请求校验会话 CSRF、Host 和 Origin；失败登录有限速。

认证写请求上限 16 KiB。登录先在 SQLite 事务中预占尝试预算再计算哈希：每用户名 8 次/15 分钟；服务级登录 120 次/分钟、注册 20 次/分钟、启用 10 次/分钟。过限返回 429 和 Retry-After，窗口结束可重试。该限制保护认证资源，不代替邀请准入、上游配额或公网流量防护。

### 受控注册

注册策略在服务启动时从环境配置读取，`GET /api/auth/config` 返回实际策略，登录页据此显示邀请码或暂停注册说明。默认 `open`、无总量上限，兼容已有本机部署；公开内测应显式采用部署示例的 `invite` 与账号上限，限流本身不能阻止长期重复注册套取赠送额度。

| 配置 | 可选值及效果 |
| --- | --- |
| `IMAGE_WORKBENCH_REGISTRATION_MODE` | `open` 开放、`invite` 一次性邀请、`closed` 暂停新注册；不影响已有账号登录 |
| `IMAGE_WORKBENCH_MAX_ACCOUNTS` | 普通账号总量上限；`0` 不设上限，管理员不计入，禁用账号仍计入 |

邀请码由可信本机维护命令签发，默认 72 小时过期；只输出一次原始值，账号库仅保存哈希、过期时间和使用记录。不通过公开 HTTP 签发。请私下交给指定试用者，不放入 URL、日志或 Git。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m image_workbench --data-root data accounts-invite --expires-hours 72
```

注册请求可携带 `invitation_code`。服务器在密码哈希前检查准入，并在账号、工作空间、初始赠送流水的同一写事务中重新核对名额、消耗邀请码。并发使用同一邀请码只能成功一次；用户名冲突不会消耗邀请或额外赠送。配置错误拒绝启动，不自动退回开放注册。修改环境后需重启 Web，不能仅改登录页来限制注册。

`moy` 在初始化时预留，普通注册不能认领。本机 `.identity/moy-activation.json` 保存一次性启用凭据，数据库仅存其哈希，设置密码后失效。启用文件写入失败回滚账号及归属事务；未启用账号缺失凭据文件时，重启服务可重新准备。此目录不能作为公开静态资源。

实现参考：[OWASP 密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、[会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、[SQLite 事务](https://www.sqlite.org/lang_transaction.html)。

## 迁移方案

本次预览：57 个主项目、57 个子项目、106 个任务、131 张图片（39 输入、92 输出），无活动作业。

1. 停止 Web/Worker，处理活动或待确认任务。
2. 运行只读 dry-run，保存指纹；预览不创建账号库。
3. 在数据根目录之外准备元数据备份；应用时另存备份，已有账号库用 SQLite backup API 备份。
4. 携指纹应用。指纹变化、活动作业、Worker 未停止或备份失败时拒绝。
5. 一个事务内创建 `moy`、默认空间、无限额及旧项目映射；重复执行不改变正确归属或重复赠送。
6. 核对原 JSON 与图片哈希、ID、路径和引用；通过后才写成功报告。
7. 启动 Web，由本人设置 `moy` 密码后登录。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m image_workbench --data-root data accounts-migrate --dry-run
.\.venv\Scripts\python.exe -X utf8 -m image_workbench --data-root data accounts-migrate --apply --expected-fingerprint <预览指纹> --backup-directory reports/account_upgrade/migration-backups
.\.venv\Scripts\python.exe -X utf8 -m image_workbench --data-root data web
```

恢复前停止服务、备份当前整个数据目录，再按迁移备份核对元数据。首次迁移只增加 `.identity`，原图片和资源元数据保持原地；退回旧版时保留新增身份目录作为恢复材料。若已经有新账号、流水或项目，不可直接以旧库覆盖，需先保全并核对新增记录。报告无 `status=applied` 和 `integrity_verified=true` 时不能视为完成。

原底层 CLI 供可信本机维护。未分配的 CLI 项目不会自动出现在任何用户空间，需通过迁移命令显式绑定；真实生成通过登录后的工作台提交。云端不再使用共享 Basic 浏览器密码，见 [远程部署说明](remote_deployment.md)。本阶段没有支付、自动充值、团队协作或复杂权限管理。
