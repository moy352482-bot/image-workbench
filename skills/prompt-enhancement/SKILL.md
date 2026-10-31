---
name: prompt-enhancement
description: 将一句普通图片需求整理成可编辑、可对比、保留原始意图的专业提示词。提供图片生成、图片修改、人物与产品一致性、电影感四个基础 Skill；只做提示词强化，不扣积分、不写文件、不调用图片生成。
---

# Prompt Enhancement

本 Skill 服务于工作台左侧创作需求区的「AI 强化提示词」。它接收原始需求、强化模式、任务类型、参考图片角色和可选的修改约束，返回：

- 原始需求 `original_prompt`
- 强化结果 `enhanced_prompt`（可编辑）
- Skill、风格与任务来源
- 修改任务的 `edit_plan`
- 失败时的原文回退与原因

## 边界

- 不创建任务、不扣积分、不写文件、不调用 `ImageBackend`。
- 不建立第二套 Model Router、TaskRunner 或 Style Library。
- 风格来自现有 `skills/image-asset-generation/styles` 注册表，只读引用。
- 每次强化默认只请求一次轻量模型；格式错误、空响应、超时均回退原文。
- 用户始终可以查看原文、编辑强化结果、恢复原文、重新强化。
- 点击「使用强化版」只替换输入框内容；真正生图仍由原有「开始生成」执行。

## 四个基础 Skill

| skill_id | 任务类型 | 作用 |
| --- | --- | --- |
| `image-generation` | generate / reference_generate | 普通文生图与参考图生成，保留主体、数量和精确文字。 |
| `image-editing` | edit | 明确修改对象、保留对象、不允许改变的属性、参考图片角色和画幅继承。 |
| `consistency` | generate / reference_generate / edit | 多参考图下锁定人物身份、产品结构与标识。 |
| `cinematic` | generate / reference_generate / edit | 用有来源的光线、空间层次和叙事焦点建立电影感。 |

Skill 定义是 `skills/*.json` 数据文件，遵循 [skill.schema.json](schemas/skill.schema.json)。选择顺序由 Python 确定性规则完成：显式 `skill_id` 优先；改图任务优先 `image-editing`；参考图身份/产品角色优先 `consistency`；电影感关键词或 `cinematic-realism` 风格优先 `cinematic`；其余使用 `image-generation`。

## 输出契约

LLM 只返回一个 JSON 对象，由 Pydantic 校验：

```json
{
  "enhanced_prompt": "完整提示词",
  "intent_summary": "一句话意图",
  "preserved_user_requirements": ["..."],
  "added_details": ["..."],
  "negative_constraints": ["..."],
  "edit_plan": null
}
```

`edit_plan` 只在 `task_type=edit` 时使用，且必须包含 `change_targets`、`preserve_targets`、`forbidden_changes`、`reference_roles`、`inherit_aspect_ratio`。用户显式填写的修改约束优先于模型建议。

## 当前实现阶段

Phase 1 使用确定性 Mock 模型验收，不产生真实模型费用。真实 `deepseek_flash` 适配器已复用现有 `config/script_routing.json` 的配置与环境变量，默认 `provider=mock`；切换到真实模型属于独立验收动作。
