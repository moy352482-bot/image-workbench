# 许可证与第三方来源

应用原创代码以仓库根目录的 [MIT License](../LICENSE) 发布，版权署名为 `2026 moy352482-bot`。

仓库内随包分发的第三方内容保留其原始许可证：

| 内容 | 来源与版本 | 随包许可记录 |
| --- | --- | --- |
| PhotoSwipe | PhotoSwipe 5.4.4 | [LICENSE](../image_workbench/web_static/vendor/photoswipe/LICENSE)、[SOURCE.json](../image_workbench/web_static/vendor/photoswipe/SOURCE.json) |
| img-comparison-slider | 8.0.7，包含已记录的 ResizeObserver 生命周期修正 | [LICENSE](../image_workbench/web_static/vendor/img-comparison-slider/LICENSE)、[SOURCE.json](../image_workbench/web_static/vendor/img-comparison-slider/SOURCE.json) |
| MiniMax H3 API 工作流 | 从 Comfy Org 官方 T2V、I2V、R2V 模板转换 | [MIT LICENSE](../workflows/minimax_h3/LICENSE)、[provenance.json](../workflows/minimax_h3/provenance.json) |
| 脚本／剧本创作方法 | drama-skills、ViMax、Toonflow 的选定创作知识，固定版本与改编范围见来源清单 | [NOTICE](../skills/video-script-generation/NOTICE.txt)、[来源](../skills/video-script-generation/references/SOURCES.json)、[licenses](../skills/video-script-generation/licenses/) |
| 分镜创作方法 | drama-skills、ViMax、Toonflow 与 MiniMax H3 的选定知识 | [NOTICE](../skills/storyboard-planning/NOTICE.txt)、[来源](../skills/storyboard-planning/references/SOURCES.json)、[licenses](../skills/storyboard-planning/licenses/) |
| H3 视频创作规则 | MiniMax H3 官方规则与项目适配层 | [H3 Community License](../skills/h3-video-workflow/LICENSE)、[NOTICE](../skills/h3-video-workflow/NOTICE)、[来源](../skills/h3-video-workflow/references/SOURCES.json) |

上述 MiniMax H3 创作规则及分镜资料中的对应改编部分保留 H3 Community License，不适用应用原创代码的 MIT 授权。图片风格预览为项目原创示例，资料来源与处理方式见 [图片 Skill 来源说明](../skills/image-generation-default/references/sources.md)。

工作流来源记录只包含官方来源地址、原始模板 SHA-256 和转换器标识。API JSON 是模板定义，不包含模板引用的示例图片、模型权重或本机 ComfyUI 数据。运行时由受控适配器替换提示词、参考资产与输出位置。

Python 依赖通过 `requirements*.txt` 声明并由使用者安装，未将其安装目录复制进本仓库。ComfyUI、Codex CLI、FFmpeg、模型权重和外部服务也不包含在发布源码中；它们保留各自的许可证、商标与使用条件。

应用的 MIT 授权不改变上传资产、模型权重或生成内容的权利归属。仓库不附带用户上传的图片资产、第三方人物图片素材、登录信息、真实配置、数据库或生成视频。

2026-10-19，仓库所有者明确要求把已部署包中的三份剧本/分镜参考 TXT 一并公开上传，文件位于 `skills/video-script-generation/examples/screenplays/` 和 `skills/video-script-generation/references/storyboard/`。这些参考正文不是应用原创代码，不因放入本仓库而自动适用应用代码的 MIT 授权；本次没有另行声明开放内容许可。冻结版本内的来源登记保留原始记录和摘要，登记中的“未声明开放再分发许可”反映导入时的状态；本段补充本次公开上传的授权记录。
