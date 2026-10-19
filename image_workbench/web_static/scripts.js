(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const uuid = () => crypto.randomUUID().replaceAll('-', '');
  const active = new Set(['queued', 'running', 'generating', 'archiving', 'checking']);
  const labels = { queued: '等待执行', running: '生成中', generating: '生成中', archiving: '归档中', checking: '归档中', completed: '已完成', succeeded: '已完成', failed: '生成失败', uncertain: '待恢复 / 确认' };
  const state = { account: null, config: null, csrf: '', projects: [], tasks: [], detail: null, parent: null, quote: null, quoteBody: '', quoteSequence: 0, selectionSequence: 0, detailSequence: 0, busy: false, refreshing: false, timer: null, pending: null, editRequest: null, editTask: null };
  const optionalFields = { duration: 'scriptDuration', aspect_ratio: 'scriptRatio', target_audience: 'scriptAudience', tone: 'scriptTone', characters: 'scriptCharacters', product_ip: 'scriptProduct', constraints: 'scriptConstraints', change_reason: 'scriptChangeReason' };
  const fieldNames = { user_request: '生成需求', input_request: '生成需求', duration: '视频时长', video_duration: '视频时长', aspect_ratio: '画面比例', target_audience: '目标受众', tone: '语气与风格', characters: '人物', product_ip: '产品 / IP', constraints: '其他约束', change_reason: '修改说明', theme: '主题', scenes: '场景', language: '语言' };
  const requestedWorkspace = new URL(location.href).searchParams.get('workspace_id');
  const validId = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
  const storageKey = suffix => 'video-scripts.' + state.account.account_id + '.' + state.account.workspace_id + '.' + suffix;
  const remembered = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const remember = (key, value) => { try { if (value) localStorage.setItem(key, value); else localStorage.removeItem(key); } catch (_) { /* Browser storage may be disabled. */ } };

  function node(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }
  function option(select, value, text, disabled = false) {
    const item = node('option', '', text); item.value = value; item.disabled = disabled; select.append(item);
  }
  function notice(message, error = false, source = 'general') {
    state.noticeSource = source;
    $('scriptNotice').textContent = message || '';
    $('scriptNotice').hidden = !message;
    $('scriptNotice').classList.toggle('is-error', error);
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const workspace = state.account?.workspace_id || requestedWorkspace;
    if (validId(workspace)) headers.set('X-Workspace-ID', workspace);
    if (options.method && options.method !== 'GET') headers.set('X-Workbench-CSRF', state.csrf);
    if (options.body) headers.set('Content-Type', 'application/json');
    let response;
    try { response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store' }); }
    catch (_) { throw new Error('无法连接工作台，请检查连接后重试。'); }
    if (response.status === 401) { location.assign('/login'); throw new Error('请重新登录'); }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      let message = typeof payload.detail === 'string' ? payload.detail : '请求未完成（' + response.status + '）';
      if (payload.status === 'credit_insufficient') message = '积分不足。当前余额：' + payload.remaining_credit + ' 积分；本次生成：' + payload.expected_credit + ' 积分；还需要：' + Math.max(0, payload.expected_credit - payload.remaining_credit) + ' 积分。';
      const error = new Error(message); error.status = response.status; throw error;
    }
    return payload;
  }
  function date(value) {
    const parsed = new Date(value);
    return value && !Number.isNaN(parsed.getTime()) ? new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(parsed) : '';
  }
  function typeName(value) { return value === 'screenplay' ? '剧本' : '脚本'; }
  function models() { return Array.isArray(state.config?.models) ? state.config.models : []; }
  function templates() { return Array.isArray(state.config?.templates) ? state.config.templates : []; }
  function selectedModel() { return models().find(item => item.model_id === $('scriptModel').value); }
  function levelName(modelId, effort) {
    const model = models().find(item => item.model_id === modelId);
    return model?.thinking_levels?.find(item => item.id === effort)?.display_name || effort || '';
  }
  function wallet() {
    const credits = state.account?.credits || state.config?.credits || {};
    const text = credits.is_unlimited ? '可用积分：无限' : '可用 ' + (credits.remaining_credit ?? 0) + ' 积分 · 冻结 ' + (credits.frozen_credit ?? 0);
    $('scriptCredits').textContent = text;
    $('scriptBalance').textContent = text;
  }
  async function refreshAccount() {
    state.account = await api('/api/auth/me');
    $('scriptAccountName').textContent = state.account.username;
    $('scriptAdminLink').hidden = !state.account.is_admin;
    wallet();
  }
  function requestBody() {
    const body = { subproject_id: $('scriptSubproject').value, generation_type: $('scriptType').value, template_id: $('scriptTemplate').value, input_request: $('scriptRequest').value.trim(), selection_mode: $('scriptModel').value === 'recommended' ? 'recommended' : 'manual' };
    if (body.selection_mode === 'manual') { body.model_id = $('scriptModel').value; body.thinking_level = $('scriptThinking').value; }
    for (const [key, id] of Object.entries(optionalFields)) if ($(id).value.trim()) body[key] = $(id).value.trim();
    if (state.parent) body.parent_version_id = state.parent.version_id;
    else delete body.change_reason;
    return body;
  }
  function readyToQuote() {
    return Boolean(state.config && $('scriptSubproject').value && $('scriptTemplate').value && $('scriptRequest').value.trim() && (!state.parent || $('scriptChangeReason').value.trim()));
  }
  function updateControls() {
    for (const item of $('scriptForm').querySelectorAll('input, textarea, select, button')) item.disabled = state.busy;
    $('scriptMainProject').disabled = state.busy || !state.projects.length;
    $('scriptSubproject').disabled = state.busy || !$('scriptSubproject').options.length;
    $('scriptWorkspace').disabled = state.busy;
    $('scriptNew').disabled = state.busy;
    $('scriptThinking').disabled = state.busy || $('scriptModel').value === 'recommended';
    $('scriptType').disabled = state.busy || Boolean(state.parent);
    $('scriptQuote').disabled = state.busy || !readyToQuote();
    $('scriptGenerate').disabled = state.busy || !state.quote?.can_generate || state.quoteBody !== JSON.stringify(requestBody());
    $('scriptContinue').disabled = $('scriptGenerate').disabled;
    $('scriptForm').setAttribute('aria-busy', String(state.busy));
  }
  function invalidateQuote(schedule = true) {
    state.quoteSequence += 1;
    state.quote = null; state.quoteBody = '';
    clearTimeout(state.timer);
    $('scriptPrice').textContent = '等待报价';
    $('scriptQuoteDetail').textContent = readyToQuote() ? schedule ? '需求已更新，正在准备新报价…' : '再次生成前请查看报价。' : '选择子项目和模板，填写需求后查看报价。';
    $('scriptWarnings').hidden = true;
    updateControls();
    if (schedule && readyToQuote()) state.timer = setTimeout(() => requestQuote(false).catch(() => {}), 650);
  }
  function renderThinking(previous = '') {
    const select = $('scriptThinking'); select.replaceChildren();
    const model = selectedModel();
    if (!model) {
      option(select, '', '根据需求推荐');
      $('scriptModelHelp').textContent = '系统推荐会结合需求和模板选择模型与思考强度。';
    } else {
      for (const level of model.thinking_levels || []) option(select, level.id, (level.display_name || level.id) + (level.pricing_status === 'unpriced' ? ' · 暂未定价' : ''));
      if ([...select.options].some(item => item.value === previous)) select.value = previous;
      $('scriptModelHelp').textContent = model.available === false ? model.unavailable_reason || '当前模型暂不可用' : '将使用你选择的模型和思考强度。';
    }
    updateControls();
  }
  function renderModels() {
    const previous = $('scriptModel').value;
    const effort = $('scriptThinking').value;
    $('scriptModel').replaceChildren(); option($('scriptModel'), 'recommended', '系统推荐');
    for (const model of models()) option($('scriptModel'), model.model_id, model.display_name + (model.available === false ? ' · 暂不可用' : ''));
    if ([...$('scriptModel').options].some(item => item.value === previous)) $('scriptModel').value = previous;
    renderThinking(effort);
  }
  function templateInfo() {
    const template = templates().find(item => item.template_id === $('scriptTemplate').value);
    $('scriptTemplateInfo').hidden = !template;
    $('scriptTemplateDescription').textContent = template?.description || '';
    const requirements = template?.input_requirements || [];
    const requirementNames = requirements.map(item => {
      const key = typeof item === 'string' ? item : item.field || item.id || '';
      return (typeof item === 'object' && (item.label || item.display_name)) || template?.input_requirement_labels?.[key] || fieldNames[key] || key;
    });
    $('scriptTemplateRequirements').textContent = requirements.length ? '建议提供：' + requirementNames.join('、') : '';
    const examples = template?.example_refs || [];
    $('scriptTemplateExamples').textContent = examples.length ? '已关联 ' + examples.length + ' 份案例，仅供结构、格式与风格参考。' : '当前模板没有关联案例。';
  }
  function renderTemplates(preferred = '') {
    const type = $('scriptType').value;
    const previous = preferred || $('scriptTemplate').value;
    $('scriptTemplate').replaceChildren();
    for (const item of templates().filter(value => (value.type || value.generation_type) === type && value.enabled !== false)) option($('scriptTemplate'), item.template_id, item.name);
    if (!$('scriptTemplate').options.length) option($('scriptTemplate'), '', '暂无可用模板');
    if ([...$('scriptTemplate').options].some(item => item.value === previous)) $('scriptTemplate').value = previous;
    $('scriptTypeHelp').textContent = type === 'screenplay' ? '剧本侧重人物、场景、动作、对白、冲突与转折，适合短剧、故事短片和微电影。' : '脚本侧重主题、创意、视觉事件、旁白与节奏，适合短视频、广告和品牌内容。';
    templateInfo();
  }
  async function requestQuote(explicit) {
    clearTimeout(state.timer);
    if (!readyToQuote()) { if (explicit) $('scriptForm').reportValidity(); return null; }
    const sequence = ++state.quoteSequence;
    const body = requestBody(); const signature = JSON.stringify(body);
    $('scriptPrice').textContent = '正在报价';
    try {
      const value = await api('/api/scripts/quote', { method: 'POST', body: JSON.stringify(body) });
      if (sequence !== state.quoteSequence || signature !== JSON.stringify(requestBody())) return null;
      state.quote = value; state.quoteBody = signature;
      if (state.noticeSource === 'quote') notice('');
      $('scriptPrice').textContent = Number.isInteger(value.price) ? value.price === 0 ? '免费试用 · 0 积分' : value.price + ' 积分' : '暂不可生成';
      const choice = (value.display_name || value.model_id || '') + ' · ' + levelName(value.model_id, value.thinking_level);
      $('scriptQuoteDetail').textContent = choice + (value.reason ? '。' + value.reason : '') + (value.shortfall > 0 ? '。积分不足，还需要 ' + value.shortfall + ' 积分。' : '');
      $('scriptBalance').textContent = value.remaining_credit === null ? '当前余额：无限积分' : '当前余额：' + value.remaining_credit + ' 积分';
      const warnings = Array.isArray(value.warnings) ? value.warnings : [];
      $('scriptWarnings').hidden = !warnings.length;
      $('scriptWarningText').textContent = warnings.join('\n');
      updateControls(); return value;
    } catch (error) {
      if (sequence !== state.quoteSequence) return null;
      state.quote = null; state.quoteBody = '';
      $('scriptPrice').textContent = '暂不可生成';
      $('scriptQuoteDetail').textContent = error.message;
      updateControls();
      if (explicit) notice(error.message, true, 'quote');
      return null;
    }
  }
  function resetResult() {
    state.detail = null; state.detailSequence += 1;
    $('scriptResultTitle').textContent = '等待你的第一份作品';
    $('scriptResultStatus').textContent = '未选择'; $('scriptResultStatus').className = 'status-pill status-neutral';
    $('scriptResultMeta').textContent = '生成的 TXT 会保存在当前项目与子项目中。';
    $('scriptText').textContent = '这里将显示脚本或剧本正文。';
    for (const id of ['scriptTaskError', 'scriptResultActions', 'scriptVersionsPanel', 'scriptRecoveryActions']) $(id).hidden = true;
  }
  function renderHistory() {
    $('scriptTaskCount').textContent = state.tasks.length + ' 条';
    $('scriptHistory').replaceChildren();
    if (!state.tasks.length) { $('scriptHistory').append(node('p', 'scripts-help', '当前子项目还没有脚本或剧本，填写需求开始创作。')); return; }
    for (const item of state.tasks) {
      const button = node('button', 'scripts-history-row' + (item.task_id === state.detail?.task_id ? ' is-selected' : '')); button.type = 'button';
      const copy = node('div');
      copy.append(node('strong', '', item.name || String(item.input_request || typeName(item.generation_type)).split('\n')[0].slice(0, 56)));
      copy.append(node('small', '', [typeName(item.generation_type), item.output_version ? 'v' + item.output_version : '', item.source === 'manual_edit' ? '手动修改' : item.model || item.model_id, date(item.created_at)].filter(Boolean).join(' · ')));
      const statusClass = item.status === 'completed' ? 'approved' : item.status === 'running' || item.status === 'archiving' ? 'generating' : item.status;
      button.append(copy, node('span', 'status-pill status-' + statusClass, labels[item.status] || item.status));
      button.addEventListener('click', () => selectTask(item.task_id).catch(error => notice(error.message, true)));
      $('scriptHistory').append(button);
    }
  }
  function downloadURL(taskId) {
    return '/api/scripts/tasks/' + encodeURIComponent(taskId) + '/download?workspace_id=' + encodeURIComponent(state.account.workspace_id);
  }
  function renderDetail(detail) {
    state.detail = detail;
    $('scriptResultTitle').textContent = typeName(detail.generation_type) + (detail.output_version ? ' · v' + detail.output_version : '');
    $('scriptResultStatus').textContent = labels[detail.status] || detail.status;
    $('scriptResultStatus').className = 'status-pill status-' + (detail.status === 'completed' ? 'approved' : active.has(detail.status) ? 'generating' : detail.status);
    $('scriptResultMeta').textContent = [detail.source === 'manual_edit' ? '手动修改 · 不扣积分' : [detail.model || detail.model_id, levelName(detail.model_id, detail.thinking_level), (detail.price ?? 0) + ' 积分'].filter(Boolean).join(' · '), date(detail.completed_at || detail.created_at)].filter(Boolean).join(' ｜ ');
    $('scriptTaskError').hidden = !detail.error; $('scriptTaskError').textContent = detail.error || '';
    $('scriptText').textContent = detail.text || (active.has(detail.status) ? '任务已保存，正文完成后会在这里显示。' : '此任务暂无可下载正文。');
    $('scriptResultActions').hidden = detail.status !== 'completed' || !detail.text;
    $('scriptDownload').href = downloadURL(detail.task_id);
    $('scriptRecover').hidden = !detail.can_recover;
    $('scriptResume').hidden = detail.status !== 'queued';
    $('scriptRecoveryActions').hidden = !detail.can_recover && detail.status !== 'queued';
    const versions = Array.isArray(detail.versions) ? detail.versions : [];
    $('scriptVersionsPanel').hidden = !versions.length; $('scriptVersions').replaceChildren();
    for (const version of [...versions].reverse()) {
      const row = node('div', 'scripts-version-row'); const copy = node('div');
      copy.append(node('strong', '', 'v' + version.version + ' · ' + date(version.created_at)));
      copy.append(node('span', '', version.change_reason || '首次生成'));
      const view = node('button', 'secondary-button', version.task_id === detail.task_id ? '当前版本' : '查看'); view.type = 'button'; view.disabled = version.task_id === detail.task_id;
      view.addEventListener('click', () => selectTask(version.task_id).catch(error => notice(error.message, true)));
      row.append(copy, view); $('scriptVersions').append(row);
    }
    renderHistory();
  }
  async function selectTask(taskId) {
    const sequence = ++state.detailSequence; const selection = state.selectionSequence;
    const detail = await api('/api/scripts/tasks/' + encodeURIComponent(taskId));
    if (sequence !== state.detailSequence || selection !== state.selectionSequence) return;
    renderDetail(detail);
    remember(storageKey('task'), taskId);
  }
  async function loadHistory(preferred = null) {
    const subproject = $('scriptSubproject').value; const selection = state.selectionSequence;
    if (!subproject) { state.tasks = []; renderHistory(); return; }
    const payload = await api('/api/scripts/tasks?subproject_id=' + encodeURIComponent(subproject));
    if (selection !== state.selectionSequence || subproject !== $('scriptSubproject').value) return;
    state.tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    renderHistory();
    const taskId = preferred || state.detail?.task_id;
    if (taskId && (state.tasks.some(item => item.task_id === taskId) || taskId === state.detail?.task_id)) await selectTask(taskId);
    else if (!state.detail && state.tasks.length) await selectTask(state.tasks[0].task_id);
  }
  function clearParent() {
    state.parent = null; $('scriptParent').hidden = true; $('scriptChangeReasonField').hidden = true;
    $('scriptChangeReason').value = ''; $('scriptChangeReason').required = false;
    $('scriptEditorTitle').textContent = '新建内容';
  }
  function changeSubproject() {
    state.selectionSequence += 1; state.tasks = []; renderHistory(); resetResult(); clearParent(); invalidateQuote();
    const project = state.projects.find(item => (item.main_project_id || item.project_id) === $('scriptMainProject').value);
    const sub = project?.subprojects?.find(item => item.subproject_id === $('scriptSubproject').value);
    $('scriptLocation').textContent = sub ? project.name + ' / ' + sub.name : '请选择子项目';
    $('scriptNoProject').hidden = Boolean(sub);
    if (state.account) { remember(storageKey('main'), $('scriptMainProject').value); remember(storageKey('sub'), sub?.subproject_id || ''); }
    loadHistory().catch(error => notice(error.message, true));
  }
  function renderSubprojects(preferred = '') {
    const project = state.projects.find(item => (item.main_project_id || item.project_id) === $('scriptMainProject').value);
    $('scriptSubproject').replaceChildren();
    for (const sub of (project?.subprojects || []).filter(item => (item.status || 'active') === 'active')) option($('scriptSubproject'), sub.subproject_id, sub.name);
    if ([...$('scriptSubproject').options].some(item => item.value === preferred)) $('scriptSubproject').value = preferred;
    changeSubproject();
  }
  async function loadProjects() {
    const payload = await api('/api/main-projects');
    state.projects = (payload.main_projects || []).filter(item => (item.status || 'active') === 'active');
    const imageSuffix = '.' + state.account.account_id + '.' + state.account.workspace_id;
    const query = new URL(location.href).searchParams;
    const previous = $('scriptMainProject').value || query.get('main_project_id') || remembered(storageKey('main')) || remembered('image-workbench.project' + imageSuffix);
    const sub = $('scriptSubproject').value || query.get('subproject_id') || remembered(storageKey('sub')) || remembered('image-workbench.subproject' + imageSuffix);
    $('scriptMainProject').replaceChildren();
    for (const project of state.projects) option($('scriptMainProject'), project.main_project_id || project.project_id, project.name);
    if ([...$('scriptMainProject').options].some(item => item.value === previous)) $('scriptMainProject').value = previous;
    renderSubprojects(sub);
  }
  async function submissionId(body) {
    const bytes = new TextEncoder().encode(JSON.stringify(body));
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const fingerprint = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
    if (!state.pending) { try { state.pending = JSON.parse(remembered(storageKey('pending')) || 'null'); } catch (_) { state.pending = null; } }
    if (!state.pending || state.pending.fingerprint !== fingerprint) state.pending = { fingerprint, request_id: uuid() };
    remember(storageKey('pending'), JSON.stringify(state.pending));
    return state.pending.request_id;
  }
  async function generate(warningsConfirmed = false) {
    if (state.busy || !$('scriptForm').reportValidity()) return;
    if (!state.quote || state.quoteBody !== JSON.stringify(requestBody())) await requestQuote(true);
    if (!state.quote?.can_generate) return;
    if (state.quote.warnings?.length && !warningsConfirmed) { $('scriptWarnings').hidden = false; $('scriptContinue').focus(); return; }
    state.busy = true; updateControls(); notice('');
    const body = JSON.parse(state.quoteBody);
    try {
      const requestId = await submissionId(body);
      const result = await api('/api/scripts/tasks', { method: 'POST', body: JSON.stringify({ ...body, request_id: requestId, quote_signature: state.quote.quote_signature, warnings_confirmed: warningsConfirmed }) });
      state.pending = null; remember(storageKey('pending'), '');
      invalidateQuote(false);
      await Promise.all([selectTask(result.task_id), refreshAccount()]);
      await loadHistory(result.task_id);
      notice('生成任务已保存。正文完成后可查看、修改和下载 TXT。');
    } catch (error) {
      notice(error.message, true);
      if (error.status === 402 || error.status === 409) invalidateQuote(false);
      await refreshAccount().catch(() => {});
    } finally { state.busy = false; updateControls(); }
  }
  function regenerate() {
    const detail = state.detail;
    if (!detail?.version_id || detail.status !== 'completed') return;
    state.parent = detail;
    $('scriptType').value = detail.generation_type; renderTemplates(detail.template_id);
    $('scriptRequest').value = detail.input_request || '';
    for (const [key, id] of Object.entries(optionalFields)) $(id).value = key === 'change_reason' ? '' : detail[key] || '';
    if (detail.model_id && models().some(item => item.model_id === detail.model_id)) $('scriptModel').value = detail.model_id;
    else $('scriptModel').value = 'recommended';
    renderThinking(detail.thinking_level || '');
    $('scriptParent').hidden = false; $('scriptParentLabel').textContent = '基于 ' + typeName(detail.generation_type) + ' v' + detail.output_version + ' 创建新版本';
    $('scriptChangeReasonField').hidden = false; $('scriptChangeReason').required = true;
    $('scriptEditorTitle').textContent = '生成修改版本';
    invalidateQuote(false); $('scriptChangeReason').focus();
    $('scriptForm').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  async function recoverOrResume(action, button) {
    if (!state.detail || button.disabled) return;
    button.disabled = true;
    try {
      const taskId = state.detail.task_id;
      await api('/api/scripts/tasks/' + encodeURIComponent(taskId) + '/' + action, { method: 'POST', body: '{}' });
      await Promise.all([selectTask(taskId), refreshAccount()]); await loadHistory(taskId);
      notice(action === 'recover' ? '正文归档已恢复，未再次调用模型。' : '已继续执行原排队任务。');
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  }
  $('scriptForm').addEventListener('input', () => invalidateQuote());
  $('scriptType').addEventListener('change', () => { renderTemplates(); invalidateQuote(); });
  $('scriptTemplate').addEventListener('change', () => { templateInfo(); invalidateQuote(); });
  $('scriptModel').addEventListener('change', () => { renderThinking(); invalidateQuote(); });
  $('scriptThinking').addEventListener('change', () => invalidateQuote());
  $('scriptForm').addEventListener('submit', event => { event.preventDefault(); generate(false); });
  $('scriptQuote').addEventListener('click', () => requestQuote(true));
  $('scriptContinue').addEventListener('click', () => generate(true));
  $('scriptModify').addEventListener('click', () => { $('scriptWarnings').hidden = true; $('scriptModel').focus(); });
  $('scriptMainProject').addEventListener('change', () => renderSubprojects());
  $('scriptSubproject').addEventListener('change', changeSubproject);
  $('scriptClearParent').addEventListener('click', () => { clearParent(); invalidateQuote(); });
  $('scriptRegenerate').addEventListener('click', regenerate);
  $('scriptRecover').addEventListener('click', event => recoverOrResume('recover', event.currentTarget));
  $('scriptResume').addEventListener('click', event => recoverOrResume('run', event.currentTarget));
  $('scriptNew').addEventListener('click', () => {
    $('scriptForm').reset(); clearParent(); renderTemplates(); renderThinking(); invalidateQuote(false);
    state.pending = null; remember(storageKey('pending'), ''); notice(''); $('scriptRequest').focus();
  });
  $('scriptWorkspace').addEventListener('change', async event => {
    try { await api('/api/workspaces/select', { method: 'POST', body: JSON.stringify({ workspace_id: event.target.value }) }); location.assign('/scripts'); }
    catch (error) { event.target.value = state.account.workspace_id; notice(error.message, true); }
  });
  $('scriptLogout').addEventListener('click', async () => {
    try { await api('/api/auth/logout', { method: 'POST' }); location.assign('/login'); }
    catch (error) { notice(error.message, true); }
  });
  $('scriptEdit').addEventListener('click', () => {
    if (!state.detail?.text) return;
    state.editTask = state.detail.task_id; state.editRequest = uuid();
    $('scriptEditedText').value = state.detail.text; $('scriptEditReason').value = '';
    $('scriptEditMessage').hidden = true; $('scriptEditMessage').textContent = '';
    $('scriptEditDialog').showModal(); $('scriptEditedText').focus();
  });
  for (const id of ['scriptEditClose', 'scriptEditCancel']) $(id).addEventListener('click', () => $('scriptEditDialog').close());
  $('scriptEditForm').addEventListener('input', () => { state.editRequest = uuid(); });
  $('scriptEditForm').addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('scriptEditSave'); if (button.disabled) return;
    for (const control of $('scriptEditForm').querySelectorAll('input, textarea, button')) control.disabled = true;
    $('scriptEditMessage').hidden = true;
    try {
      const result = await api('/api/scripts/tasks/' + encodeURIComponent(state.editTask) + '/revisions', { method: 'POST', body: JSON.stringify({ request_id: state.editRequest, text: $('scriptEditedText').value, change_reason: $('scriptEditReason').value.trim() }) });
      $('scriptEditDialog').close(); await selectTask(result.task_id); await loadHistory(result.task_id);
      notice('新版本已保存，原版继续保留。本次未调用模型、未扣积分。');
    } catch (error) { $('scriptEditMessage').textContent = error.message; $('scriptEditMessage').hidden = false; }
    finally { for (const control of $('scriptEditForm').querySelectorAll('input, textarea, button')) control.disabled = false; }
  });
  async function refresh() {
    if (state.refreshing || state.busy) return;
    state.refreshing = true;
    try {
      await refreshAccount(); state.config = await api('/api/scripts/config');
      renderModels(); renderTemplates(); invalidateQuote(false);
      await loadHistory();
    } finally { state.refreshing = false; }
  }
  $('scriptRefresh').addEventListener('click', () => refresh().catch(error => notice(error.message, true)));
  (async () => {
    state.csrf = (await api('/api/auth/config')).csrf_token;
    await refreshAccount();
    state.config = await api('/api/scripts/config'); state.csrf = state.config.csrf_token || state.csrf;
    for (const item of state.account.workspaces || []) option($('scriptWorkspace'), item.workspace_id, item.name);
    $('scriptWorkspace').value = state.account.workspace_id;
    const types = Array.isArray(state.config.generation_types) ? state.config.generation_types : [];
    if (types.length) { $('scriptType').replaceChildren(); for (const item of types) option($('scriptType'), item.id, item.name); }
    renderModels(); renderTemplates(); await loadProjects(); wallet(); updateControls();
    setInterval(async () => {
      if (document.hidden || state.busy || state.refreshing || !state.account || !state.tasks.some(item => active.has(item.status))) return;
      state.refreshing = true;
      try { await loadHistory(); await refreshAccount(); }
      catch (error) { notice(error.message, true); }
      finally { state.refreshing = false; }
    }, 5000);
  })().catch(error => { notice(error.message, true); $('scriptGenerate').disabled = true; $('scriptQuote').disabled = true; });
})();
