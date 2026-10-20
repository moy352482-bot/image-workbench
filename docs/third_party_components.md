# 本地前端组件

| 项目 | 版本与来源 | 本地位置 | 许可 |
| --- | --- | --- | --- |
| [PhotoSwipe](https://github.com/dimsemenov/PhotoSwipe/tree/v5.4.4) | 5.4.4 官方发布文件 | `image_workbench/web_static/vendor/photoswipe/` | [MIT 原文](../image_workbench/web_static/vendor/photoswipe/LICENSE) |
| [img-comparison-slider](https://github.com/sneas/img-comparison-slider/tree/img-comparison-slider-v8.0.7) | 8.0.7 官方 npm 包，核对完整性后保留原版与本地修补版 | `image_workbench/web_static/vendor/img-comparison-slider/` | [MIT 原文](../image_workbench/web_static/vendor/img-comparison-slider/LICENSE) |

PhotoSwipe 原始文件及 SHA-256 见 [来源记录](../image_workbench/web_static/vendor/photoswipe/SOURCE.json)。只使用 core，单图按需加载；不安装 Lightbox 包或任何远程 CDN。

对比组件的 [SOURCE.json](../image_workbench/web_static/vendor/img-comparison-slider/SOURCE.json) 记录 npm 完整性、原版及修补版 SHA-256、两处准确的源码替换。`index.upstream.js` 是上游原版；实际 `index.js` 只增加 ResizeObserver 保存与断开，其他上游行为保持。接入层另处理中文界面、加载错误、键盘步进、窗口与图片资源清理。更新组件时须重核补丁和验收，不覆盖原来源记录冒充未修改。

分发网站静态资源时保留以上目录内的完整 LICENSE。前端文件随现有代码发布包一起交付，无需 npm 构建。供应链来源校验和本地行为测试不能保证组件不存在尚未公开的问题。
