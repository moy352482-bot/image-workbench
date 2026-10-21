# 电商套图小规模批量验收用例

`suite.json` 为自包含清单，遵循 schema_version 1，按纯生成、参考图生成、编辑和异常边界各 4 项组织。正常用例共 12 项；边界用例共 4 项，预期在准备阶段以 `prepare_failed` 结束，不调用模型。清单中的图片路径均相对本目录。

## 分组与链路

- `pure_generation`：短/长提示词、方形/横向/纵向比例覆盖。
- `reference_generation`：单主体商品主图、单张风格参考场景图、主体详情特写，以及主体/风格/构图多参考。
- `edit`：四代睫毛膏同项目编辑链；首项引用成功参考生成，后续逐项引用上一代 `case_id`。
- `boundary`：非法类型、损坏 PNG、缺失输入及空提示词；缺失图 `assets/missing.png` 故意不创建。

## 参考素材

`assets/sources.json` 记录三张从 DummyJSON 公开商品样例 API/CDN 获取的原始图片来源、下载日期、SHA-256 和尺寸。图片作为本地测试输入保存，未编辑；此记录不表示图片已获开源许可或可用于公开发布。

本目录不包含真实模型调用脚本。由集成执行框架读取清单并负责运行；4个 `expected_failure` 用例均使用真实输入校验触发拒绝，不在执行器里伪造失败。

补充：每个 expected_aspect_ratio 均同步写入 prompt。ref_fragrance_scene 是单张 style 风格参考；ref_mascara_main/ref_phone_detail 是单张主体参考；ref_multi_cosmetic_campaign 是三张多参考。背景编辑从白色改为浅米色，后续编辑沿用浅米色。

`suite_repeat.json` 选取3项代表用例，用于真实重复批测：纯生成、单主体参考、损坏图负例。应有2次模型调用，负例不调用模型。首轮最后一次编辑未返回本地图片路径，因此编辑复测另以原 task_id 单独执行，保留原失败记录。合计额外复测4项、最多3次模型请求。
