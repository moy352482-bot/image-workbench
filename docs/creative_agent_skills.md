# 脚本与分镜 Agent 内置创作方法

2026-10-22：将上游创作知识选择性改写为两套本地 Skill，接入现有文本生成框架。没有安装上游执行框架，也不新增模型调用阶段。

| 框架 | 实际加载的规则 | 职责与产物 |
| --- | --- | --- |
| 脚本／剧本：ScriptService → ScriptTemplateLoader → CodexTextAdapter | video-script-generation/SKILL.md + 按 generation_type 选择的 script-craft.md 或 screenplay-craft.md | 主题与可见事件，人物目标、因果、冲突、对白、节奏、改写；输出 TXT |
| 分镜：StoryboardService → StoryboardPlanner → CodexTextAdapter | storyboard-planning/SKILL.md + shot-design.md + continuity.md | 镜头职责、时长、景别、主体／摄影机动作、空间轴线、首尾状态、参考绑定；输出现有 JSON |

脚本框架的自由生成、系统模板和个人模板都使用同一套类型匹配的内置方法。风格和案例保持独立资料层，用户明确约束优先。分镜规则读取已保存的脚本版本，不能擅自重写关键剧情。H3 媒体执行继续使用独立的 h3-video-workflow；本次两套方法只负责文本创作与规划。

## 来源拆解

| 来源 | 脚本／剧本侧采用 | 分镜侧采用 | 未引入 |
| --- | --- | --- | --- |
| zenstory-ai/drama-skills | short-drama-write 的人物目标、场景变化、因果及对白方法 | short-drama-storyboard 的镜头作用、站位、证据可读性和多人调度 | 外部文件工作流、固定审批步骤、全套工具操作 |
| HKUDS/ViMax | script_planner 中的叙事推进与可视化表达原则 | storyboard_artist 中的静态首帧、动作与结束状态设计 | AutoGen 编排、模型客户端、媒体生成与文件执行代码 |
| HBAI-Ltd/Toonflow-app | scriptCraft 的场景、动机、节奏和修订知识 | shotCraft/directorPlan 的导演意图、镜头信息和空间连续性 | 画布工具、平台存储、供应商与任务系统 |
| MiniMax-AI/MiniMax-H3 | 不将其媒体制作流程混入写作输入 | 3d-animation-short-generator 中的空间锚点、身份绑定、声音归属与跨镜交接 | MiniMax Hub 调用、逐秒强制表格、固定风格、固定爆点密度与时长上限 |

每套 Skill 的 references/SOURCES.json 记录具体文件、固定提交和 SHA-256；NOTICE.txt 和 licenses/ 保留来源及许可。MiniMax 的改编部分保留 H3 Community License 声明，不标成 MIT。没有复制 huobao-drama 的非商业许可内容。仅采用已审阅的创作知识，没有执行下载的代码，也不把上游的 Shell、网络、凭据访问、模型默认值或自动调用指令带入宿主。

## 提交、版本与发布

脚本以前主要通过代码规则和模板生成 Prompt；现在 SKILL.md 与对应专项方法实际进入模型输入。分镜以前只读取主 SKILL.md；现在连同两份方法参考一起编译。来源清单与许可证用于归属记录，不进入生成输入。

规则文件的完整内容参与摘要，引用文件改动也会使新提交的旧报价失效。脚本沿用 resource_references.skill、creative_snapshot 和 generation_prompt 保存规则来源及完整输入；分镜任务增加可选 skill_reference 并沿用 prompt 保存完整输入。同一次提交的报价与输入编译使用同一份规则快照。排队执行和恢复仍使用已保存 Prompt，不临时读取新规则；已有无 skill_reference 的历史任务继续兼容。

脚本完成后自动接续分镜的既有流程，在分镜任务受理时冻结当时规则；先前组合报价继续锁定已确认模型与价格。它不是在脚本提交时预先冻结未来分镜 Prompt。常规分镜提交和局部重规划仍校验当前完整规则摘要与报价签名。

运行所需参考及许可证纳入发布文件集合，缺少必需文件时打包失败。服务端规则缺失时，在冻结积分和调用模型前失败。发布脚本的变化只用于保证包完整，本次不执行部署。

## 架构与兼容

本次属于 Skill 内容与现有 Application Service 的输入编译：只使用各自现有文本适配器、StorageManager、Workspace 校验、积分预约和持久队列，依赖方向不变，不增加 Core 特例或新模块。没有新数据库模型；新增来源元数据为可选字段，不改变 URL、生成输出字段、磁盘正文格式、权限、价格、幂等或结算规则。

回滚只需恢复对应代码、Skill 和发布清单，不迁移业务数据；保留已排队任务的完整 Prompt 和历史版本。测试使用临时目录及假适配器，验证类型分流、自由／个人模板、实际 Prompt、引用变更导致重新报价、缺失资源在扣款前失败、排队输入冻结、重试幂等及发布资源完整性，并运行 scripts/storyboards 模块检查。Mock 能证明接入和事务行为，不能证明真实模型的文学质量提升。
