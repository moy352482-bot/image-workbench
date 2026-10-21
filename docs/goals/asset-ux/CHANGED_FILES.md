# 本次交付文件

依据本地集成清单生成。仅列本轮合入主目录的变更；同目录其他任务的独立修改保留。测试截图另存 `screenshots/`。

## 业务与界面

- `image_workbench/accounts.py`
- `image_workbench/asset_library.py`
- `image_workbench/atomic_files.py`
- `image_workbench/capacity.py`
- `image_workbench/composition.py`
- `image_workbench/contracts/scripts.py`
- `image_workbench/http_api/assets.py`
- `image_workbench/http_api/schemas.py`
- `image_workbench/http_api/tasks.py`
- `image_workbench/http_api/uploads.py`
- `image_workbench/http_api/workspace.py`
- `image_workbench/image_queries.py`
- `image_workbench/image_references.py`
- `image_workbench/pipeline_service.py`
- `image_workbench/remote_queue.py`
- `image_workbench/script_models.py`
- `image_workbench/script_service.py`
- `image_workbench/script_web.py`
- `image_workbench/storage.py`
- `image_workbench/storyboard_service.py`
- `image_workbench/task_runner.py`
- `image_workbench/video_pipeline.py`
- `image_workbench/video_store.py`
- `image_workbench/video_web.py`
- `image_workbench/web_static/asset-actions.js`
- `image_workbench/web_static/asset-input.css`
- `image_workbench/web_static/asset-input.js`
- `image_workbench/web_static/pipeline.js`
- `image_workbench/web_static/script-pricing.js`
- `image_workbench/web_static/scripts.css`
- `image_workbench/web_static/scripts.js`
- `image_workbench/web_static/storyboard.js`
- `image_workbench/web_static/video.js`
- `image_workbench/web_static/workbench-accounts.js`
- `image_workbench/web_static/workbench-api.js`
- `image_workbench/web_static/workbench.js`
- `image_workbench/web_templates/index.html`
- `image_workbench/web_templates/scripts.html`
- `image_workbench/web_templates/video.html`
- `image_workbench/workbench_state.py`

## 测试与检查

- `architecture.json`
- `scripts/verify.py`
- `tests/contracts/http_routes.json`
- `tests/test_asset_browser.py`
- `tests/test_asset_e2e_browser.py`
- `tests/test_asset_inputs.py`
- `tests/test_asset_storyboard_browser.py`
- `tests/test_asset_tray.py`
- `tests/test_asset_tray_browser.py`
- `tests/test_atomic_replace.py`
- `tests/test_pipeline_service.py`
- `tests/test_script_direct_submit.py`
- `tests/test_script_generation_browser.py`
- `tests/test_script_limits.py`

## 文档与证据

- `docs/execution_contract.md`
- `docs/goals/asset-ux/BLOCKERS.md`
- `docs/goals/asset-ux/browser-A.json`
- `docs/goals/asset-ux/browser-B.json`
- `docs/goals/asset-ux/browser-C.json`
- `docs/goals/asset-ux/browser-D.json`
- `docs/goals/asset-ux/browser-storyboard.json`
- `docs/goals/asset-ux/COMPLETION_AUDIT.md`
- `docs/goals/asset-ux/DECISIONS.md`
- `docs/goals/asset-ux/FINAL_REPORT.md`
- `docs/goals/asset-ux/INTEGRATION_MAP.md`
- `docs/goals/asset-ux/PRICING_AUDIT.md`
- `docs/goals/asset-ux/PROGRESS.md`
- `docs/goals/asset-ux/TEST_REPORT.md`
- `docs/storage_contract.md`
- `docs/web_api_contract.md`

## 回滚与检查

原文件备份：`work/asset-ux-20261020/root-before-integration/`；SHA-256 集成清单：`work/asset-ux-20261020/local-integration.json`。仅记录本次改动，不包含业务 data 目录。
