# Luna 舰队真实生图批测清单

本目录提供 12 份彼此独立、每份 20 项的 `schema_version: 1` 清单。前 10 份共 200 项为主测，后 2 份共 40 项为备用；每项预期 `success`，全部是正常图片任务，不含真实负例。每个任务要求生成或编辑一张图片。

每份清单固定包含 10 项纯生成、5 项参考图生成、5 项编辑，总计 120 项纯生成、60 项参考图生成、60 项编辑。系列覆盖商品主图与场景、图文海报、插画、室内、自然、成年人像、旅行户外、餐饮、动物、服饰配饰、工作学习和建筑城市；方形 `1:1`、横向 `3:2`、竖向 `2:3` 均有覆盖。

## 清单

| 序号 | 用途 | 主题 | 清单 |
|---:|---|---|---|
| 01 | 主测 | 商品主图与场景 | [suite.json](01_product/suite.json) |
| 02 | 主测 | 图文海报 | [suite.json](02_poster/suite.json) |
| 03 | 主测 | 插画创作 | [suite.json](03_illustration/suite.json) |
| 04 | 主测 | 室内空间 | [suite.json](04_interior/suite.json) |
| 05 | 主测 | 自然风景 | [suite.json](05_nature/suite.json) |
| 06 | 主测 | 成年人像 | [suite.json](06_adult_portrait/suite.json) |
| 07 | 主测 | 旅行与户外 | [suite.json](07_travel/suite.json) |
| 08 | 主测 | 餐饮与烘焙 | [suite.json](08_food/suite.json) |
| 09 | 主测 | 动物与宠物 | [suite.json](09_animals/suite.json) |
| 10 | 主测 | 服饰与配饰 | [suite.json](10_fashion/suite.json) |
| 11 | 备用 | 工作与学习场景 | [suite.json](11_worklife/suite.json) |
| 12 | 备用 | 建筑与城市 | [suite.json](12_architecture/suite.json) |

每个目录都包含自己的 `assets/` 和 `suite.json`，可单独传给批测执行器。参考素材只复制自 `tests/manual_batch_cases/assets/` 已有的三张 WEBP；每份目录也保留原 `sources.json` 来源说明。没有增加外部图片或下载新素材。

## 编辑依赖

每份清单的 5 项编辑都依赖同项目中的前序任务。编辑 01、02、03 形成两代深度的连续编辑与历史分支；编辑 04、05 构成另一条短分支。两条支路分别从前面的参考图生成 01、02 起步，降低单个基底失败影响整份编辑集的程度。

## 执行

在项目根目录按需逐份运行现有批测命令，例如：

```powershell
python -m image_workbench batch --manifest tests/fleet_cases/01_product/suite.json --backend codex --allow-image-generation
```

上述命令会真实调用 Codex 生图并使用账号额度；启动前确认环境、权限与额度已准备好。此目录本身只提供清单和参考素材。批次是否执行成功、输出质量如何，需以执行器报告为准。
## 参考图使用与修订记录

参考图生成任务以 prompt 中的文字作为目标主体和场景。参考图只用于借鉴灯光、材质质感与配色，不应带入参考图的物体、商品造型、包装文字或商标。

实测主测第 11 项时发现 `01_product_ref_01` 将银色腕表目标与睫毛膏/香水参考图的“主体外观与包装比例”描述混用，存在目标与参考素材不一致的问题。`01_product` 当时正在执行，因此保留原清单以避免改变运行中的任务；本次已修订尚未执行的 `02_poster` 至 `12_architecture` 中全部 55 个参考图任务，统一明确上述风格参考边界。该修订不回写或掩盖已发现的问题。
