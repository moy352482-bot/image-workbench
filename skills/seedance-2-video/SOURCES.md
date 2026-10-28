# 提示词规则来源

核对日期：2026-10-28。

- BytePlus 官方 Seedance 2.0 提示词指南：https://docs.byteplus.com/en/docs/modelark/seedance-2-0-prompt-guide
- 火山引擎官方指南：https://www.volcengine.com/docs/82379/2222480?lang=zh
- 官方 sd2-pe Skill 下载入口：https://arkdoc.tos-cn-beijing.volces.com/files/video-generation/SKILL.md
- 研究中发现的社区实现：https://github.com/mqrox/seedance-2.0-prompt-skill 。它也指向上述官方资料，并记录官方下载文件未附明确再分发许可。

本站 SKILL.md 为独立编写的适配规则，未复制第三方 Skill 正文、示例或代码，也没有安装社区可执行程序。文生视频参数取自本站已接入的 online-2.0 通道，不能据官方完整模型能力推断当前通道支持参考图或其他输入。

DeepSeek 接口核对：https://api-docs.deepseek.com/api/create-chat-completion/ 。使用 `deepseek-flash`，支持 low/high/max 思考强度。剧本和分镜继续加载原有 Skill；本规则只用于 SD2.0 视频提示词整理。
