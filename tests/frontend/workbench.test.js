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

test('live execution time advances only running snapshots and keeps server results authoritative', () => {
  const { WorkbenchFormat: format } = component('workbench-format.js');
  const detail = { status: 'generating', elapsed_seconds: 59, execution_seconds: 59 };
  assert.equal(format.executionTimingText(detail, 1), '生成中 · 已运行 1 分 0 秒');
  assert.equal(format.executionTimingText(detail, 61), '生成中 · 已运行 2 分 0 秒');
  assert.equal(detail.elapsed_seconds, 59, 'display interpolation must not change the server snapshot');
  assert.equal(format.executionTimingText({ ...detail, status: 'queued' }, 30), '等待执行 · 尚未开始计时');
  assert.equal(format.executionTimingText({ ...detail, status: 'failed', execution_seconds: 12 }, 30), '执行失败 · 本次执行耗时 12 秒');
  for (const elapsed_seconds of [null, undefined, NaN, -1]) {
    assert.equal(format.executionTimingText({ ...detail, elapsed_seconds }, 30), '生成中 · 已运行 —');
  }
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
