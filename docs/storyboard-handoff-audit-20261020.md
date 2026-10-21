# 素材与分镜交接独立复核

复核完成时间：2026-10-20 23:14（Asia/Shanghai）。

发现 1 个 P2 问题：**选中镜头 A 时，把电脑图片文件拖到镜头 B，图片会错误绑定到 A，保存后仍是 A。** 暂存区图片跨镜头拖拽正常。其余本次检查的粘贴、保存刷新、脚本版本关联和视频 Artifact 绑定路径通过。

本次只增加复核脚本、证据和本文档；未修改业务代码，未部署或重启服务，未调用付费模型，未写入生产业务数据。

## P2：电脑文件拖入另一镜头时绑定到旧选择

复现条件：分镜至少有两个镜头；当前选中 A；将电脑里的 PNG/JPG/WEBP 文件拖到 B 的镜头行。

实际结果：

1. 上传成功，素材产生有效 Artifact。
2. 当前选择仍是 A，新增参考图也出现在 A。
3. 点击“保存选中镜头”后，读取正式分镜版本，A 的 `reference_artifact_ids` 包含新 Artifact，B 仍为空。

这会使用户把参考图保存到错误镜头。问题已在独立 Chromium 用例中两次复现；素材库里的原图没有丢失。

原因位于 [storyboard.js](../image_workbench/web_static/storyboard.js#L112)：电脑文件的 drop 监听安装在整个分镜面板，随后 [asset-input.js](../image_workbench/web_static/asset-input.js#L110) 调用共享 `receive(files, purpose)`，上下文取的是当前选择。镜头行上的 `IWAssetActions.dropzone` 只处理暂存区专用 MIME，不处理文件 `Files`，因此没有把落点镜头传入上传上下文。

建议修复：文件落在镜头行时，在上传前固定该行的镜头 ID、分镜版本与项目上下文；复用已有未保存编辑保护。不能保留当前编辑时应明确阻止切换，不能静默附加到另一个镜头。

证据：

- [失败用例与堆栈](../work/storyboard-handoff-audit-20261020/test-results-extra.json)
- [落点、实际选择及保存结果](../work/storyboard-handoff-audit-20261020/file-drop-other-row.json)
- [复现截图](../work/storyboard-handoff-audit-20261020/screenshots/file-drop-other-row.png)
- [可重跑的独立复核脚本](../work/storyboard-handoff-audit-20261020/run_audit.py)

原交接的 `test_tray_drag_to_storyboard_reuses_artifact_without_new_image_file` 使用的默认 Mock 脚本只生成一个镜头，因此取“最后一个镜头”时仍是默认镜头。本次明确创建第二个镜头，分别验证了电脑文件和暂存区引用两条拖入路径。这是原测试的覆盖缺口，不是原测试执行失败。

## 已通过的重点路径

| 检查 | 结果与证据范围 |
| --- | --- |
| 粘贴到默认选中分镜 | 保留未保存画面文案；上传后保存、刷新仍是同一 Artifact；旧分镜版本未被改写。 |
| 保留首尾帧和提示词 | 新增粘贴参考图时，未保存的首帧、尾帧和视频提示词均保留；保存、刷新后仍一致。 |
| 上传期间切换镜头 | 图片入原项目素材库，不会绑定到新选中的镜头，也不会误写正式分镜。 |
| 电脑文件拖到当前选中镜头 | 上传、保存、刷新通过。拖到另一镜头存在上述 P2。 |
| 暂存区图片拖到另一镜头 | 双镜头实测绑定到落点镜头；原镜头不变；保存刷新通过；Artifact ID 不变，未增加图片文件。 |
| 历史脚本生成分镜 | 先创建脚本 v2，再点击 v1 菜单；报价、提交及分镜记录均引用 v1 的版本 ID 和 TXT Artifact。 |
| 历史脚本保存模板 | 读取并保存 v1 文本、Artifact、版本号及原工作空间、子项目归属。 |
| 视频 Artifact 归档 | Mock Worker 产物登记 MP4 Artifact；下载字节与哈希一致；同卷硬链接复用原视频内容。 |
| 视频与分镜绑定 | 草稿保留具体分镜版本、分镜 JSON Artifact、稳定镜头 ID 和来源脚本版本；执行途中重排两个镜头，完成后仍绑定原镜头及原版本。 |
| 已完成视频后的编辑 | 单纯重排不标过期；修改镜头内容后旧视频标过期，旧结果仍可读取。 |
| 暂存视频绑定草稿 | 持久保存相同 Artifact，刷新后可预览；不复制视频文件、不触发生成收费；已有视频及已提交目标不能被覆盖。 |

测试使用临时 SQLite/文件存储、真实本地 HTTP 应用、Chromium 和 Mock 适配器。浏览器粘贴/拖拽使用真实 `File`、`DataTransfer` 对象触发应用事件；没有人工操作操作系统剪贴板或资源管理器，也没有在生产账号中执行编辑流程。

## 检查统计及复跑

现有项目检查 **192 项全部通过**。独立补充 **6 项，5 项通过、1 项确认产品缺陷**。合计 198 个不同检查，197 项通过、1 项失败、无跳过；其中浏览器用例 24 项，23 项通过、1 项失败。架构检查为 79 个模块、397 条导入、0 违规。没有把失败用例标为预期失败或降低断言。

首次独立运行有两项在“必须至少两个镜头”的夹具断言处停止；改为明确创建第二个镜头后重跑独立用例，最终统计采用修正后的结果。首次日志和 JSON 保留，避免混淆夹具问题与产品问题。原项目 192 项检查未受此影响。

所选检查包含 `scripts/verify.py --module storyboards` 对应的全部模块和公共检查，再加素材输入、暂存区、视频及 Worker 相关检查；独立运行器仅将浏览器报告输出重定向到本次证据目录，未覆盖交接报告。没有宣称本次重跑了交接报告中的 639 项完整检查。

在项目目录复跑全部本次检查：

```powershell
.venv\Scripts\python.exe -X utf8 work\storyboard-handoff-audit-20261020\run_audit.py
```

仅复跑 6 项独立用例：

```powershell
.venv\Scripts\python.exe -X utf8 work\storyboard-handoff-audit-20261020\run_audit.py --extras-only
```

缺陷未修复时，两条命令均应因跨镜头文件拖入断言失败返回退出码 1。

[汇总证据](../work/storyboard-handoff-audit-20261020/summary.json) · [原检查日志](../work/storyboard-handoff-audit-20261020/tests.log) · [最终独立检查日志](../work/storyboard-handoff-audit-20261020/tests-extra.log)

## 本地与线上一致性

于 2026-10-20 23:08:29 +08:00，通过现有 SSH 配置只读核对：

- `/opt/image-workbench/current` 指向 `20261020T110008Z-e3ef56a0`。
- 本地发布范围 151 个文件与远端实际文件 SHA-256 完全一致，无缺失、额外文件或字节差异；远端实际文件与 `RELEASE.json` 清单也一致。
- 服务 `active/running`，启动时间 2026-10-20 19:00:50 CST。
- 公网 `/login`、`/api/auth/config` 及 5 个相关前端脚本均返回 HTTP 200；前端脚本响应字节与本地一致，TLS 证书验证通过。
- 测试前后源文件未变化；收尾时再次确认当前本地发布文件哈希与本次线上核验哈希一致。

因此，线上发布文件中也包含此次发现的拖入处理逻辑。本次没有在生产环境操作分镜来复现缺陷。SSH 账号无权读取 `/proc/<MainPID>/cwd`，未能直接核实进程启动时的真实工作目录；未提升权限或绕过该限制。结论覆盖发布文件、公开静态响应和服务状态，不等同于检查所有运行中 Python 对象。

[线上核验 JSON](../work/storyboard-handoff-audit-20261020/live-verification.json) · [只读核验脚本](../work/storyboard-handoff-audit-20261020/verify_live.py)
