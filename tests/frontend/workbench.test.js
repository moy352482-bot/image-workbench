const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

function component(name, environment = {}) {
  const context = vm.createContext({ window: {}, Headers, FormData, ...environment });
  vm.runInContext(readFileSync(resolve(__dirname, '../../image_workbench/web_static', name), 'utf8'), context);
  return context.window;
}

test('API reads current workspace/CSRF for each write and preserves caller headers', async () => {
  const { WorkbenchApi } = component('workbench-api.js');
  let workspace = 'one', csrf = 'first';
  const calls = [];
  const api = WorkbenchApi.create({
    getWorkspaceId: () => workspace, getCsrfToken: () => csrf, onUnauthorized: () => {},
    fetchImpl: async (path, options) => {
      calls.push({ path, ...options });
      return { ok: true, status: 200, json: async () => ({ saved: true }) };
    },
  });
  const headers = new Headers({ 'X-Test': 'kept' });
  await api('/api/tasks', { method: 'POST', body: '{}', headers });
  workspace = 'two'; csrf = 'second';
  await api('/api/tasks', { method: 'POST', body: new FormData() });
  assert.equal(calls[0].headers.get('X-Workspace-ID'), 'one');
  assert.equal(calls[1].headers.get('X-Workspace-ID'), 'two');
  assert.equal(calls[1].headers.get('X-Workbench-CSRF'), 'second');
  assert.equal(calls[0].headers.get('Content-Type'), 'application/json');
  assert.equal(calls[1].headers.has('Content-Type'), false, 'browser must supply the multipart boundary');
  assert.equal(headers.has('X-Workbench-CSRF'), false, 'caller-owned Headers must not be mutated');
  assert.equal(calls[0].cache, 'no-store');
});

test('unauthorized responses invalidate previews, errors stay readable, 204 has no JSON body', async () => {
  const { WorkbenchApi } = component('workbench-api.js');
  let invalidated = 0, response;
  const api = WorkbenchApi.create({
    getWorkspaceId: () => null, getCsrfToken: () => '',
    onUnauthorized: () => { invalidated++; }, fetchImpl: async () => response,
  });
  response = { ok: false, status: 401, json: async () => ({}) };
  await assert.rejects(api('/private'), /请重新登录/);
  assert.equal(invalidated, 1);
  response = { ok: false, status: 409, json: async () => ({ detail: '任务已改变' }) };
  await assert.rejects(api('/changed'), /任务已改变/);
  response = { ok: true, status: 204, json: () => { throw Error('must not parse'); } };
  assert.equal(await api('/empty'), null);
});

test('network failure is translated without exposing fetch implementation details', async () => {
  const { WorkbenchApi } = component('workbench-api.js');
  const api = WorkbenchApi.create({
    getWorkspaceId: () => null, getCsrfToken: () => '', onUnauthorized: () => {},
    fetchImpl: async () => { throw Error('internal-host:port'); },
  });
  await assert.rejects(api('/offline'), /无法连接本地工作台/);
});

test('execution time preserves explicit unknown values and queue semantics', () => {
  const { WorkbenchFormat: format } = component('workbench-format.js');
  assert.equal(format.executionDuration({ execution_seconds: null, timings: { total_seconds: 600 } }), null);
  assert.equal(format.executionDuration({ timings: { total_seconds: 2 } }), 2);
  assert.equal(format.executionTimingText({ status: 'queued', elapsed_seconds: 50 }), '等待执行 · 尚未开始计时');
  assert.equal(format.formatSeconds(61), '1 分 1 秒');
  assert.equal(format.formatSeconds(undefined), '—');
});

test('credit state text follows backend billing status and never guesses', () => {
  const { WorkbenchFormat: format } = component('workbench-format.js');
  assert.equal(format.creditStateText('waiting_credit', 40), '预计消耗');
  assert.equal(format.creditStateText('credit_frozen', 40), '已冻结');
  assert.equal(format.creditStateText('running', 40), '已冻结');
  assert.equal(format.creditStateText('completed', 40), '已扣除');
  assert.equal(format.creditStateText('refunded', 40), '已退回');
  assert.equal(format.creditStateText('credit_insufficient', 40), '预计消耗');
  assert.equal(format.creditStateText('completed', 0), '免费');
  assert.equal(format.creditStateText(undefined, 40), '扣费状态待确认');
  assert.equal(format.creditStateText('credit_unknown', 40), '扣费状态待确认');
  assert.equal(format.creditStateText('completed', null), '已扣除');
  assert.equal(format.creditAmountText('credit_frozen', 40), '40');
  assert.equal(format.creditAmountText('credit_unknown', 40), '待确认');
  assert.equal(format.creditAmountText(undefined, 40), '待确认');
  assert.equal(format.creditAmountText('completed', null), '待确认');
});

function historySample(id, seconds, overrides = {}) {
  return {
    task_id: id, operation: 'generate', status: 'awaiting_review',
    execution_seconds: seconds, execution_timing_source: 'execution_record',
    execution_backend: 'codex', input_count: 0, base_artifact_id: null, ...overrides,
  };
}

test('estimate requires enough measured successful samples', () => {
  const { WorkbenchEstimate } = component('workbench-estimate.js');
  const result = WorkbenchEstimate.estimateGenerationTime({
    detail: { task_id: 'current', operation: 'generate', input_count: 0, base_artifact_id: null },
    backend: 'codex',
    tasks: [
      historySample('a', 30), historySample('b', 40),
      historySample('c', null), historySample('d', 25, { status: 'failed' }),
      historySample('e', 25, { status: 'queued' }),
      historySample('f', 25, { execution_timing_source: null }),
      historySample('k', 25, { execution_timing_source: 'job_clock' }),
      historySample('g', 25, { operation: 'edit' }),
      historySample('h', 25, { execution_backend: 'local' }),
      historySample('i', Number.NaN), historySample('j', -3),
    ],
  });
  assert.equal(result.available, false);
  assert.equal(result.text, '暂无足够历史数据');
  assert.equal(result.source, '至少需要 3 次同类完成任务');
  assert.equal(result.sampleCount, 2);
});

test('estimate averages same-type history and shows a range for wide spread', () => {
  const { WorkbenchEstimate } = component('workbench-estimate.js');
  const estimate = tasks => WorkbenchEstimate.estimateGenerationTime({
    detail: { task_id: 'current', operation: 'generate', input_count: 0, base_artifact_id: null },
    backend: 'codex', tasks,
  });
  const close = estimate([historySample('a', 36), historySample('b', 38), historySample('c', 40)]);
  assert.equal(close.text, '约 38 秒');
  assert.equal(close.low, 38);
  assert.equal(close.high, 38);
  assert.equal(close.source, '基于历史同类任务 · 3 次');
  const wide = estimate([historySample('a', 30), historySample('b', 38), historySample('c', 46)]);
  assert.equal(wide.text, '约 30–45 秒');
  assert.equal(wide.low, 30);
  assert.equal(wide.high, 45);
  assert.equal(wide.sampleCount, 3);
});

test('estimate narrows to the current reference count when enough samples exist', () => {
  const { WorkbenchEstimate } = component('workbench-estimate.js');
  const result = WorkbenchEstimate.estimateGenerationTime({
    detail: { task_id: 'current', operation: 'generate', input_count: 1, base_artifact_id: null },
    backend: 'codex',
    tasks: [
      historySample('a', 10), historySample('b', 20),
      historySample('c', 30, { input_count: 1 }),
      historySample('d', 38, { input_count: 1 }),
      historySample('e', 46, { input_count: 1 }),
    ],
  });
  assert.equal(result.text, '约 30–45 秒');
  assert.equal(result.sampleCount, 3);
  assert.match(result.basis, /参考图数量/);
});

test('estimate narrows to the base image resolution for edit tasks', () => {
  const { WorkbenchEstimate } = component('workbench-estimate.js');
  const portrait = { width: 720, height: 1024 };
  const landscape = { width: 1536, height: 1024 };
  const result = WorkbenchEstimate.estimateGenerationTime({
    detail: {
      task_id: 'current', operation: 'edit', input_count: 0,
      base_artifact_id: 'base', base_image: portrait,
    },
    backend: 'codex',
    tasks: [
      historySample('a', 30, { operation: 'edit', base_artifact_id: 'x', preview_artifact: portrait }),
      historySample('b', 40, { operation: 'edit', base_artifact_id: 'x', preview_artifact: portrait }),
      historySample('c', 50, { operation: 'edit', base_artifact_id: 'x', preview_artifact: portrait }),
      historySample('d', 100, { operation: 'edit', base_artifact_id: 'x', preview_artifact: landscape }),
      historySample('e', 110, { operation: 'edit', base_artifact_id: 'x', preview_artifact: landscape }),
      historySample('f', 120, { operation: 'edit', base_artifact_id: 'x', preview_artifact: landscape }),
    ],
  });
  assert.equal(result.text, '约 30–50 秒');
  assert.equal(result.sampleCount, 3);
  assert.match(result.basis, /分辨率/);
});

test('estimate matches normalized remote backends and excludes the current task', () => {
  const { WorkbenchEstimate } = component('workbench-estimate.js');
  const result = WorkbenchEstimate.estimateGenerationTime({
    detail: { task_id: 'current', operation: 'generate', input_count: 0, base_artifact_id: null },
    backend: 'codex',
    tasks: [
      historySample('current', 900),
      historySample('a', 28, { execution_backend: 'remote-codex', execution_timing_source: 'job_clock' }),
      historySample('b', 30, { execution_backend: 'remote-codex', execution_timing_source: 'job_clock' }),
      historySample('c', 32, { execution_backend: 'remote-codex', execution_timing_source: 'job_clock' }),
    ],
  });
  assert.equal(result.available, true);
  assert.equal(result.sampleCount, 3);
  assert.equal(result.text, '约 30 秒');
});

test('closing a preview invalidates an in-flight fallback and restores focus', async () => {
  const elements = new Map(), events = {};
  function node(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      addEventListener: (event, handler) => { events[selector + event] = handler; },
      removeAttribute: () => {}, setAttribute: () => {}, append: () => {},
    });
    return elements.get(selector);
  }
  const dialog = { open: false, addEventListener: (event, handler) => { events[event] = handler; },
    close() { this.open = false; events.close(); },
    showModal() { this.open = true; } };
  let resolveViewer, focused = 0;
  const window = { addEventListener: () => {}, WorkbenchImageViewer: {
    close: () => {}, open: () => new Promise(resolve => { resolveViewer = resolve; }),
  } };
  const { WorkbenchPreview } = component('workbench-preview.js', { window });
  const preview = WorkbenchPreview.create({ query: node, dialog });
  const pending = preview.openLightbox({ url: '/image' }, null);
  preview.closeImagePreviews();
  resolveViewer(false);
  await pending;
  assert.equal(dialog.open, false, 'stale preview must not reopen after workspace switch');
  window.WorkbenchImageViewer.open = async () => false;
  await preview.openLightbox({ url: '/image', download_url: '/download' }, {
    isConnected: true, focus: () => { focused++; },
  });
  assert.equal(dialog.open, true);
  preview.closeImagePreviews();
  assert.equal(dialog.open, false);
  assert.equal(focused, 1);
});

test('prompt enhancement builds edit roles and preserves user text without generating', () => {
  const { WorkbenchPromptEnhancement: enhancement } = component('workbench-prompt-enhancement.js');
  const built = enhancement.buildRequest({
    requestId: 'a'.repeat(32),
    prompt: '  把背景换成海边，保持人物的脸不变  ',
    mode: 'standard',
    operation: 'edit',
    ratio: '3:2',
    baseArtifact: { artifact_id: 'base', filename: 'base.png' },
    referenceArtifacts: [
      { artifact_id: 'ref', filename: 'face.png' },
      { artifact_id: 'base', filename: 'duplicate-base.png' },
    ],
  });
  assert.equal(built.error, undefined);
  assert.equal(built.payload.prompt, '把背景换成海边，保持人物的脸不变');
  assert.equal(built.payload.task_type, 'edit');
  assert.equal(built.payload.mode, 'standard');
  assert.equal(built.payload.aspect_ratio, '3:2');
  assert.equal(built.payload.edit.inherit_aspect_ratio, true);
  assert.equal(JSON.stringify(built.payload.references.map(item => [item.index, item.role, item.artifact_id])),
    JSON.stringify([[1, 'edit_target', 'base'], [2, 'other', 'ref']]));
  assert.ok(built.payload.references.every(item => item.usage));
  assert.equal('operation' in built.payload, false, 'the enhancement endpoint must not create a task');
});

test('prompt enhancement refuses blank, oversized and baseless edit requests locally', () => {
  const { WorkbenchPromptEnhancement: enhancement } = component('workbench-prompt-enhancement.js');
  const base = { requestId: 'b'.repeat(32), mode: 'standard', operation: 'generate', ratio: 'auto' };
  assert.match(enhancement.buildRequest({ ...base, prompt: '   ' }).error, /请先写下/);
  assert.match(enhancement.buildRequest({ ...base, prompt: 'x'.repeat(4001) }).error, /4000/);
  assert.match(enhancement.buildRequest({ ...base, prompt: '改背景', operation: 'edit' }).error, /基底图片/);
  assert.match(enhancement.buildRequest({ ...base, prompt: '图', mode: 'unknown' }).error, /强化方式/);
  assert.match(enhancement.buildRequest({ ...base, prompt: '图', requestId: 'bad' }).error, /请求标识/);
  const generated = enhancement.buildRequest({
    ...base, prompt: '一只猫', referenceArtifacts: [{ artifact_id: 'r', filename: 'r.png' }],
  });
  assert.equal(generated.payload.task_type, 'generate');
  assert.equal(generated.payload.references.length, 1);
  assert.equal(generated.payload.references[0].role, 'other');
});

test('prompt enhancement usage text reports mock calls and only configured costs', () => {
  const { WorkbenchPromptEnhancement: enhancement } = component('workbench-prompt-enhancement.js');
  assert.match(enhancement.describeUsage({ usage: { is_mock: true, calls: 1 } }), /演练模型 · 调用 1 次/);
  const costed = enhancement.describeUsage({
    usage: { is_mock: false, model: 'deepseek-flash', calls: 1, input_tokens: 12,
      output_tokens: 8, estimated_cost: 0.000024, currency: 'USD' },
  });
  assert.match(costed, /模型 deepseek-flash/);
  assert.match(costed, /Token 12 \/ 8/);
  assert.match(costed, /0\.000024 USD/);
  const unknown = enhancement.describeUsage({ usage: { is_mock: false, model: 'm', calls: 0 } });
  assert.doesNotMatch(unknown, /成本|Token/);
});

function promptElement(overrides = {}) {
  const classes = new Set();
  return {
    hidden: false, disabled: false, value: '', textContent: '', checked: false,
    handlers: {},
    classList: { add: name => classes.add(name), remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name) },
    addEventListener(event, handler) { this.handlers[event] = handler; },
    setAttribute() {},
    ...overrides,
  };
}

function promptEnhancementHarness(response, context = {}) {
  const trigger = promptElement();
  const output = promptElement();
  const apply = promptElement();
  const restore = promptElement();
  const retry = promptElement();
  const close = promptElement();
  const status = promptElement();
  const panel = promptElement({ hidden: true });
  const original = promptElement();
  const meta = promptElement();
  const standard = promptElement({ checked: true, value: 'standard' });
  const creative = promptElement({ value: 'creative' });
  const nodes = { '#promptEnhanceButton': trigger, '#promptEnhancementResult': panel,
    '#promptEnhancementStatus': status, '#promptEnhancementMeta': meta,
    '#promptEnhancementOriginal': original, '#promptEnhancementOutput': output,
    '#promptEnhancementApply': apply, '#promptEnhancementRestore': restore,
    '#promptEnhancementRetry': retry, '#promptEnhancementClose': close };
  const root = { querySelector: selector => nodes[selector], querySelectorAll: () => [standard, creative] };
  const query = selector => selector === '#promptEnhancement' ? root : nodes[selector];
  const calls = [];
  let applied = [];
  const { WorkbenchPromptEnhancement } = component('workbench-prompt-enhancement.js');
  const handle = WorkbenchPromptEnhancement.create({
    query,
    requestId: () => 'c'.repeat(32),
    enhance: async payload => { calls.push(payload); return response; },
    getContext: () => ({ prompt: '一间咖啡馆', operation: 'generate', ratio: 'auto', ...context }),
    onApply: text => applied.push(text),
  });
  return { handle, trigger, output, apply, restore, retry, status, panel, original, meta, calls, applied };
}

test('prompt enhancement applies only editable text and never triggers generation', async () => {
  const response = {
    status: 'enhanced', original_prompt: '一间咖啡馆', enhanced_prompt: '一间被晨光照亮的咖啡馆',
    mode: 'standard', task_type: 'generate', skill: { name: '图片生成' }, warnings: [],
    fallback: null, usage: { is_mock: true, calls: 1 },
  };
  const harness = promptEnhancementHarness(response);
  await harness.trigger.handlers.click();
  assert.equal(harness.calls.length, 1);
  assert.equal(harness.panel.hidden, false);
  assert.equal(harness.original.textContent, '一间咖啡馆');
  assert.equal(harness.output.value, '一间被晨光照亮的咖啡馆');
  assert.equal(harness.apply.disabled, false);
  harness.apply.handlers.click();
  assert.deepEqual(harness.applied, ['一间被晨光照亮的咖啡馆']);
  assert.equal(harness.calls.length, 1, 'using the enhanced text must not call the model or submit a task');
  assert.match(harness.status.textContent, /点击“开始生成”后才会生成/);
  harness.restore.handlers.click();
  assert.deepEqual(harness.applied, ['一间被晨光照亮的咖啡馆', '一间咖啡馆']);
  assert.equal(harness.calls.length, 1);
});

test('prompt enhancement shows a bounded fallback and keeps the original editable', async () => {
  const response = {
    status: 'fallback', original_prompt: '一张产品图', enhanced_prompt: '一张产品图',
    mode: 'creative', task_type: 'generate', skill: { name: '图片生成' }, warnings: [],
    fallback: { code: 'timeout', message: '提示词强化超时，已保留原文。' },
    usage: { is_mock: true, calls: 1 },
  };
  const harness = promptEnhancementHarness(response, { prompt: '一张产品图' });
  await harness.trigger.handlers.click();
  assert.match(harness.status.textContent, /超时/);
  assert.equal(harness.output.value, '一张产品图');
  assert.equal(harness.apply.disabled, false, 'the user can still edit and use the original manually');
  harness.apply.handlers.click();
  assert.deepEqual(harness.applied, ['一张产品图']);
  assert.equal(harness.calls.length, 1);
  assert.equal(harness.handle.snapshot(), null, 'a fallback keeps the prompt but has no enhancement provenance');
  harness.handle.reset();
  assert.equal(harness.panel.hidden, true);
  assert.equal(harness.output.value, '');
});

test('prompt enhancement records original text and source only for applied enhanced results', async () => {
  const source = { kind: 'prompt_enhancement', enhancement_id: 'e'.repeat(32), skill_id: 'image-generation',
    skill_version: 2, mode: 'standard', task_type: 'generate' };
  const response = {
    status: 'enhanced', original_prompt: '一间咖啡馆', enhanced_prompt: '一间被晨光照亮的咖啡馆',
    mode: 'standard', task_type: 'generate', skill: { name: '图片生成' }, warnings: [], source,
    fallback: null, usage: { is_mock: true, calls: 1 },
  };
  const harness = promptEnhancementHarness(response);
  assert.equal(harness.handle.snapshot(), null);
  await harness.trigger.handlers.click();
  assert.equal(harness.handle.snapshot(), null, 'viewing a result does not attribute the task');
  harness.apply.handlers.click();
  assert.deepEqual(JSON.parse(JSON.stringify(harness.handle.snapshot())), {
    original_prompt: '一间咖啡馆',
    prompt_source: source,
  });
  harness.restore.handlers.click();
  assert.equal(harness.handle.snapshot(), null, 'restoring the original removes enhancement provenance');
  harness.apply.handlers.click();
  assert.equal(harness.handle.snapshot(), null, 'applying the original text is not an enhanced source');
  harness.output.value = '一间被晨光照亮的咖啡馆';
  harness.apply.handlers.click();
  assert.notEqual(harness.handle.snapshot(), null);
  harness.handle.reset();
  assert.equal(harness.handle.snapshot(), null);
});

test('prompt enhancement refuses locally before any network call for an edit without a base image', async () => {
  const harness = promptEnhancementHarness({}, { operation: 'edit' });
  await harness.trigger.handlers.click();
  assert.equal(harness.calls.length, 0);
  assert.match(harness.status.textContent, /基底图片/);
});
