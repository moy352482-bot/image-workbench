const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

class MiniNode {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.dataset = {}; this.attributes = {}; this.listeners = {}; this.style = {};
    this._text = ''; this._value = ''; this.disabled = false; this.checked = false;
    this.hidden = false; this.className = ''; this.type = ''; this.isConnected = false;
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value ?? ''); }
  get value() {
    if (this.tagName === 'SELECT' && this._value === '') return this.options[0]?.value || '';
    return this._value;
  }
  set value(value) { this._value = String(value ?? ''); }
  get options() { return this.tagName === 'SELECT' ? this.children.filter(child => child.tagName === 'OPTION') : []; }
  get childElementCount() { return this.children.length; }
  append(...nodes) { for (const child of nodes) { if (child == null) continue; if (typeof child === 'string') { this._text += child; continue; } child.remove(); child.parentNode = this; child.isConnected = this.isConnected; this.children.push(child); } }
  before(other) { if (!this.parentNode) return; const parent = this.parentNode; const index = parent.children.indexOf(this); other.remove(); other.parentNode = parent; other.isConnected = parent.isConnected; parent.children.splice(index, 0, other); }
  after(other) { if (!this.parentNode) return; const parent = this.parentNode; const index = parent.children.indexOf(this); other.remove(); other.parentNode = parent; other.isConnected = parent.isConnected; parent.children.splice(index + 1, 0, other); }
  replaceChildren(...nodes) { for (const child of this.children) child.parentNode = null; this.children = []; this._text = ''; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; this.isConnected = false; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  async fire(type, event = {}) {
    event.type = type; event.target ||= this; event.currentTarget = this; event.defaultPrevented ||= false;
    event.preventDefault ||= function () { this.defaultPrevented = true; };
    event.stopImmediatePropagation ||= function () { this.immediateStopped = true; };
    for (const callback of this.listeners[type] || []) { await callback(event); if (event.immediateStopped) break; }
    return event;
  }
  click() { return this.fire('click'); }
  dispatchEvent(event) { this.fire(event.type, event); return true; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  closest(selector) { for (let current = this; current; current = current.parentNode) if (selector.startsWith('.') && current.className.split(/\s+/).includes(selector.slice(1))) return current; return null; }
  contains(other) { if (this === other) return true; return this.children.some(child => child.contains(other)); }
  querySelectorAll(selector) {
    const all = [];
    const visit = parent => { for (const child of parent.children) { all.push(child); visit(child); } };
    visit(this);
    if (selector === 'input') return all.filter(child => child.tagName === 'INPUT');
    if (selector === 'input,textarea,select') return all.filter(child => ['INPUT','TEXTAREA','SELECT'].includes(child.tagName));
    if (selector === '[role="status"]') return all.filter(child => child.getAttribute('role') === 'status');
    if (selector === '[data-storyboard-draft]') return all.filter(child => child.dataset.storyboardDraft !== undefined);
    if (selector === '.storyboard-shot button[aria-pressed]') return all.filter(child => child.tagName === 'BUTTON' && child.getAttribute('aria-pressed') !== null && child.closest('.storyboard-shot'));
    return [];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() {}
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) { const result = find(child, predicate); if (result) return result; }
  return null;
}

const byText = (root, text) => find(root, item => item.tagName === 'BUTTON' && item.textContent === text);
const byLabel = (root, label) => find(root, item => item.getAttribute('aria-label') === label);
const tick = () => new Promise(resolve => setImmediate(resolve));

function shot(id, overrides = {}) {
  return { shot_id: id, order: 1, title: `镜头 ${id}`, status: 'planned', narrative_duration_ms: 4000,
    generation_duration_ms: 5000, video_model_id: 'h3', aspect_ratio: '16:9', video_prompt: `prompt ${id}`,
    visual_description: `画面 ${id}`, action: '走进房间', camera_framing: 'medium_shot', camera_movement: 'static',
    emotion: '', dialogue: '', voiceover: '', reference_artifact_ids: [], start_frame_artifact_id: null,
    end_frame_artifact_id: null, duration_compatibility: { supported: true }, ...overrides };
}

function harness({ boards, assets = [], taskResults = {}, makeReplanTask = null }) {
  const root = new MiniNode('main'); root.isConnected = true;
  const ids = ['videoShotForm','videoShotName','videoShotPrompt','videoDuration','videoRatio','videoMode','videoReferenceChoices','videoResolution'];
  const elements = new Map(ids.map(id => [id, new MiniNode(id === 'videoShotForm' ? 'form' : id === 'videoShotPrompt' ? 'textarea' : id === 'videoReferenceChoices' ? 'div' : id === 'videoDuration' ? 'input' : id === 'videoRatio' || id === 'videoMode' || id === 'videoResolution' ? 'select' : 'input')]));
  for (const [id, element] of elements) { element.id = id; root.append(element); }
  elements.get('videoDuration').value = '5'; elements.get('videoRatio').value = 'landscape'; elements.get('videoMode').value = 'T2V'; elements.get('videoResolution').value = '720p';
  for (const asset of assets) { const input = new MiniNode('input'); input.type = 'checkbox'; input.value = asset.video_asset_id; elements.get('videoReferenceChoices').append(input); }
  const documentListeners = {};
  const document = {
    activeElement: null,
    createElement: tag => new MiniNode(tag),
    getElementById: id => elements.get(id) || null,
    addEventListener: (type, callback) => { (documentListeners[type] ||= []).push(callback); },
    async fire(type, event = {}) { event.type = type; event.target ||= document; for (const callback of documentListeners[type] || []) await callback(event); },
  };
  const records = new Map(boards.map(board => [board.storyboard_id, { ...board, versions: [{ version: board.version, status: board.status }] }]));
  const taskMap = new Map(Object.entries(taskResults));
  const calls = [];
  const host = { state: { account: { account_id: 'account', workspace_id: 'workspace' }, project: { project_id: 'project' }, referenceOrder: [] },
    notice: message => { host.notices.push(message); }, notices: [], refresh: async () => {} };
  host.snapshot = () => structuredClone(host.state);
  host.captureContext = () => ({id: host.state.project?.project_id, workspace: host.state.account?.workspace_id});
  host.isCurrent = context => context.id === host.state.project?.project_id && context.workspace === host.state.account?.workspace_id;
  host.setReferences = ids => { host.state.referenceOrder = [...ids]; return true; };
  host.api = async (url, options = {}) => {
    const method = options.method || 'GET'; const body = options.body ? JSON.parse(options.body) : null; calls.push({ url, method, body });
    if (url === '/api/storyboards/config') return { mock_enabled: true, real_enabled: false, models: [], video_models: [{ model_id: 'h3' }] };
    if (url.startsWith('/api/storyboards?')) return { storyboards: [...records.values()].map(r => ({ storyboard_id: r.storyboard_id, version: r.version, status: r.status })) };
    if (url.endsWith('/assets')) return { assets };
    const taskMatch = url.match(/^\/api\/storyboards\/tasks\/([^/]+)$/);
    if (taskMatch) return taskMap.get(taskMatch[1]) || { task_id: taskMatch[1], status: 'running' };
    const boardMatch = url.match(/^\/api\/storyboards\/([^/?]+)(?:\?version=(\d+))?$/);
    if (boardMatch) return records.get(decodeURIComponent(boardMatch[1]));
    if (method === 'POST' && url.endsWith('/replan/quote')) return { can_generate: true, price: 0, model_id: 'mock' };
    if (method === 'POST' && url.endsWith('/replan')) {
      const taskId = (body.request_id || '').slice(0, 32) || 'a'.repeat(32);
      const result = makeReplanTask ? makeReplanTask({ taskId, body, storyboardId: url.split('/')[3] }) : { task_id: taskId, request_id: body.request_id, storyboard_id: url.split('/')[3], status: 'running' };
      taskMap.set(taskId, result); return result;
    }
    if (method === 'POST' && url.endsWith('/edit')) {
      const boardId = decodeURIComponent(url.split('/')[3]); const previous = records.get(boardId);
      const updated = { ...previous, version: previous.version + 1, shots: previous.shots.map(item => item.shot_id === body.shot_id ? { ...item, ...body.changes } : item), versions: [...previous.versions, { version: previous.version + 1, status: previous.status }] };
      records.set(boardId, updated); return updated;
    }
    if (method === 'POST' && url.endsWith('/accept')) return {};
    if (method === 'POST' && url.endsWith('/video-draft')) return {};
    throw new Error(`Unexpected API request ${method} ${url}`);
  };
  const context = vm.createContext({ window: { IWVideo: host }, document, sessionStorage: {
    values: new Map(), getItem(key) { return this.values.get(key) ?? null; }, setItem(key, value) { this.values.set(key, String(value)); }, removeItem(key) { this.values.delete(key); },
  }, crypto: { randomUUID: () => `${Math.random().toString(16).slice(2).padEnd(32, '0')}` }, Event: class { constructor(type) { this.type = type; } },
  setTimeout: callback => { (contextTimers.push(callback)); return contextTimers.length; }, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {}, });
  const contextTimers = [];
  vm.runInContext(readFileSync(resolve(__dirname, '../../image_workbench/web_static/storyboard.js'), 'utf8'), context);
  return { root, elements, records, taskMap, calls, host, context, contextTimers, document, async start() { await document.fire('video-ready'); await tick(); await tick(); }, };
}

async function selectBoard(h, id) {
  const select = byLabel(h.root, '分镜记录'); select.value = id; await select.fire('change'); await tick(); await tick();
}

async function quoteAndConfirm(h) {
  const panel = find(h.root, item => item.className === 'storyboard-panel');
  await byText(panel, '查看重新规划报价').fire('click');
  await byText(panel, '确认报价，生成候选预览').fire('click');
  await tick();
  return h.calls.filter(call => call.method === 'POST' && call.url.endsWith('/replan')).at(-1)?.body;
}

test('passive project refresh can load later storyboard versions repeatedly', async () => {
  const h = harness({ boards: [{storyboard_id:'board-a',version:1,status:'approved',shots:[shot('a1')],target_duration_ms:4000}] });
  await h.start();
  for (const version of [2, 3]) {
    const record = h.records.get('board-a');
    h.records.set('board-a', {...record, version, versions:[...record.versions,{version,status:'approved'}]});
    await h.document.fire('video-project-view', {detail:{project:{project_id:'project'}}});
    await tick(); await tick();
    assert.equal(Number(byLabel(h.root, '历史版本（只读）').value), version);
  }
});

test('same replan body on different storyboard records receives independent request IDs', async () => {
  const h = harness({ boards: [
    { storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('a1')], target_duration_ms: 4000 },
    { storyboard_id: 'board-b', version: 1, status: 'approved', shots: [shot('b1')], target_duration_ms: 4000 },
  ] });
  await h.start();
  const panel = find(h.root, item => item.className === 'storyboard-panel');
  byLabel(panel, '规划范围').value = 'all'; byLabel(panel, '规划原因').value = '相同理由';
  const first = await quoteAndConfirm(h);
  await selectBoard(h, 'board-b');
  byLabel(panel, '规划范围').value = 'all'; byLabel(panel, '规划原因').value = '相同理由';
  const second = await quoteAndConfirm(h);
  assert.equal(first.reason, second.reason);
  assert.notEqual(first.request_id, second.request_id, 'different boards must not share a replay identity');
});

test('preview task is recovered by board after switching to another board and back', async () => {
  const preview = { storyboard_id: 'board-a', version: 1, status: 'draft', shots: [shot('candidate-shot')] };
  const h = harness({ boards: [
    { storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('a1')], target_duration_ms: 4000 },
    { storyboard_id: 'board-b', version: 1, status: 'approved', shots: [shot('b1')], target_duration_ms: 4000 },
  ] });
  await h.start();
  const panel = find(h.root, item => item.className === 'storyboard-panel');
  byLabel(panel, '规划范围').value = 'all'; byLabel(panel, '规划原因').value = '暂未完成';
  const pending = await quoteAndConfirm(h); const taskId = pending.request_id.slice(0, 32);
  h.taskMap.set(taskId, { task_id: taskId, request_id: pending.request_id, storyboard_id: 'board-a', status: 'completed', preview });
  await selectBoard(h, 'board-b');
  const boardBTask = await quoteAndConfirm(h);
  assert.notEqual(boardBTask.request_id, pending.request_id);
  await selectBoard(h, 'board-a');
  assert.ok(byText(panel, '接受此候选，保存为新版本'), 'returning to board A should restore its candidate preview');
});

test('discarding a candidate permits a fresh request with the same parameters', async () => {
  const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('a1')], target_duration_ms: 4000 }],
    makeReplanTask: ({ taskId, body }) => ({ task_id: taskId, request_id: body.request_id, storyboard_id: 'board-a', status: 'completed', preview: { storyboard_id: 'board-a', version: 1, shots: [shot('candidate')] } }) });
  await h.start();
  const panel = find(h.root, item => item.className === 'storyboard-panel');
  byLabel(panel, '规划范围').value = 'all'; byLabel(panel, '规划原因').value = '相同规划';
  const first = await quoteAndConfirm(h);
  await byText(panel, '放弃此候选').fire('click');
  await byText(panel, '查看重新规划报价').fire('click'); await byText(panel, '确认报价，生成候选预览').fire('click'); await tick();
  const replans = h.calls.filter(call => call.method === 'POST' && call.url.endsWith('/replan'));
  assert.equal(replans.length, 2); assert.notEqual(replans[0].body.request_id, replans[1].body.request_id);
});

test('unknown replan response replays the same request ID', async () => {
  const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('a1')], target_duration_ms: 4000 }] });
  await h.start(); const panel = find(h.root, item => item.className === 'storyboard-panel');
  byLabel(panel, '规划范围').value = 'all'; byLabel(panel, '规划原因').value = '网络结果未知';
  await byText(panel, '查看重新规划报价').fire('click');
  const confirm = byText(panel, '确认报价，生成候选预览');
  const attemptedIds = [];
  const originalApi = h.host.api;
  h.host.api = async (url, options = {}) => {
    if (url.endsWith('/replan')) {
      const id = JSON.parse(options.body).request_id; attemptedIds.push(id);
      const result = await originalApi(url, options);
      if (attemptedIds.length === 1) throw Error('网络结果未知');
      return result;
    }
    return originalApi(url, options);
  };
  await confirm.fire('click'); await confirm.fire('click');
  assert.equal(attemptedIds.length, 2);
  assert.equal(attemptedIds[0], attemptedIds[1]);
});

for (const terminal of ['failed', 'cancelled']) test(`${terminal} replan task can be retried with identical inputs`, async () => {
  const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('a1')], target_duration_ms: 4000 }],
    makeReplanTask: ({ taskId, body }) => ({ task_id: taskId, request_id: body.request_id, storyboard_id: 'board-a', status: terminal }) });
  await h.start(); const panel = find(h.root, item => item.className === 'storyboard-panel');
  byLabel(panel, '规划范围').value = 'all'; byLabel(panel, '规划原因').value = `重试 ${terminal}`;
  await quoteAndConfirm(h);
  await byText(panel, '查看重新规划报价').fire('click'); await byText(panel, '确认报价，生成候选预览').fire('click'); await tick();
  const replans = h.calls.filter(call => call.method === 'POST' && call.url.endsWith('/replan'));
  assert.equal(replans.length, 2); assert.notEqual(replans[0].body.request_id, replans[1].body.request_id);
});

test('ratio-only video form change is saved into the storyboard shot before draft creation', async () => {
  const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('a1')], target_duration_ms: 4000 }] });
  await h.start(); const panel = find(h.root, item => item.className === 'storyboard-panel');
  await byText(panel, '1. 镜头 a1 · 4s / 生成 5s').fire('click');
  h.elements.get('videoRatio').value = 'portrait';
  const event = await h.elements.get('videoShotForm').fire('submit'); await tick();
  assert.equal(event.defaultPrevented, true);
  const edit = h.calls.find(call => call.method === 'POST' && call.url.endsWith('/edit'));
  assert.equal(edit?.body.changes.aspect_ratio, '9:16');
  assert.equal(h.calls.some(call => call.url.endsWith('/video-draft')), false, 'a changed shot must be saved before creating a video draft');
});

test('video workflow selection maps ordered video asset IDs back to Storyboard Artifact IDs', async () => {
  const assets = [
    { artifact_id: 'artifact-a', video_asset_id: 'video-a', available: true },
    { artifact_id: 'artifact-b', video_asset_id: 'video-b', available: true },
  ];
  for (const scenario of [
    { mode: 'I2V', shot: shot('s', { start_frame_artifact_id: 'artifact-a', end_frame_artifact_id: 'artifact-b' }), order: ['video-a','video-b'], expected: ['artifact-a','artifact-b'] },
    { mode: 'R2V', shot: shot('s', { reference_artifact_ids: ['artifact-b','artifact-a'] }), order: ['video-b','video-a'], expected: ['artifact-a','artifact-b'] },
    { mode: 'T2V', shot: shot('s'), order: [], expected: [] },
  ]) {
    const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [scenario.shot], target_duration_ms: 4000 }], assets });
    await h.start(); const panel = find(h.root, item => item.className === 'storyboard-panel');
    await byText(panel, '1. 镜头 s · 4s / 生成 5s').fire('click');
    assert.equal(h.elements.get('videoMode').value, scenario.mode);
    assert.deepEqual(Array.from(h.host.state.referenceOrder), scenario.order);
    if (scenario.mode === 'R2V') {
      h.host.state.referenceOrder = ['video-a','video-b'];
      await h.elements.get('videoShotForm').fire('submit'); await tick();
      const edit = h.calls.find(call => call.method === 'POST' && call.url.endsWith('/edit'));
      assert.deepEqual(edit?.body.changes.reference_artifact_ids, scenario.expected);
    }
  }
});

test('T2V to I2V saves selected start and end Artifact IDs', async () => {
  const assets = [
    { artifact_id: 'artifact-a', video_asset_id: 'video-a', available: true },
    { artifact_id: 'artifact-b', video_asset_id: 'video-b', available: true },
  ];
  const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('s')], target_duration_ms: 4000 }], assets });
  await h.start(); const panel = find(h.root, item => item.className === 'storyboard-panel');
  await byText(panel, '1. 镜头 s · 4s / 生成 5s').fire('click');
  h.elements.get('videoMode').value = 'I2V'; h.host.state.referenceOrder = ['video-b','video-a'];
  await h.elements.get('videoShotForm').fire('submit'); await tick();
  const edit = h.calls.find(call => call.method === 'POST' && call.url.endsWith('/edit'));
  assert.equal(edit?.body.changes.start_frame_artifact_id, 'artifact-b');
  assert.equal(edit?.body.changes.end_frame_artifact_id, 'artifact-a');
  assert.deepEqual(edit?.body.changes.reference_artifact_ids, []);
});

test('I2V to T2V clears both frame Artifact IDs and saves the version', async () => {
  const assets = [
    { artifact_id: 'artifact-a', video_asset_id: 'video-a', available: true },
    { artifact_id: 'artifact-b', video_asset_id: 'video-b', available: true },
  ];
  const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('s', { start_frame_artifact_id: 'artifact-a', end_frame_artifact_id: 'artifact-b' })], target_duration_ms: 4000 }], assets });
  await h.start(); const panel = find(h.root, item => item.className === 'storyboard-panel');
  await byText(panel, '1. 镜头 s · 4s / 生成 5s').fire('click');
  h.elements.get('videoMode').value = 'T2V'; h.host.state.referenceOrder = [];
  await h.elements.get('videoShotForm').fire('submit'); await tick();
  const edit = h.calls.find(call => call.method === 'POST' && call.url.endsWith('/edit'));
  assert.equal(edit?.body.changes.start_frame_artifact_id, '');
  assert.equal(edit?.body.changes.end_frame_artifact_id, '');
  assert.deepEqual(edit?.body.changes.reference_artifact_ids, []);
  assert.equal(h.records.get('board-a').version, 2, 'mode conversion is a new storyboard version');
});

test('missing Artifact reference is reported and cannot be silently dropped from a bound video draft', async () => {
  const h = harness({ boards: [{ storyboard_id: 'board-a', version: 1, status: 'approved', shots: [shot('a1', { reference_artifact_ids: ['missing-artifact'] })], target_duration_ms: 4000 }] });
  await h.start(); const panel = find(h.root, item => item.className === 'storyboard-panel');
  await byText(panel, '1. 镜头 a1 · 4s / 生成 5s').fire('click');
  h.elements.get('videoMode').value = 'T2V';
  await h.elements.get('videoShotForm').fire('submit'); await tick();
  assert.equal(h.calls.some(call => call.url.endsWith('/video-draft')), false);
  const status = panel.querySelector('[role="status"]');
  assert.match(status?.textContent || '', /缺失|不存在|不可用|missing/i);
});
