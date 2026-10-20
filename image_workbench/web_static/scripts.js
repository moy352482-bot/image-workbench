(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const uuid = () => crypto.randomUUID().replaceAll('-', '');
  const active = new Set(['queued', 'running', 'generating', 'archiving', 'checking']);
  const labels = { queued: '等待执行', running: '生成中', generating: '生成中', archiving: '归档中', checking: '归档中', completed: '已完成', succeeded: '已完成', failed: '生成失败', uncertain: '待恢复 / 确认' };
  const state = { account: null, config: null, csrf: '', projects: [], tasks: [], detail: null, parent: null, quote: null, quoteBody: '', quoteSequence: 0, selectionSequence: 0, detailSequence: 0, busy: false, refreshing: false, timer: null, pending: null, editRequest: null, editTask: null };
  const optionalFields = { duration: 'scriptDuration', aspect_ratio: 'scriptRatio', target_audience: 'scriptAudience', tone: 'scriptTone', characters: 'scriptCharacters', product_ip: 'scriptProduct', constraints: 'scriptConstraints', change_reason: 'scriptChangeReason' };
  Object.assign(state, { examples: [], exampleRefs: new Map(), styles: new Set(), variables: {}, conflictsConfirmed: false, creativeSequence: 0, examplesSequence: 0, recommendationSequence: 0, resourceSequence: 0, exampleDetail: null, templateEdit: null, draftId: null });
  const referenceRoles = { structure: '结构', style: '风格', dialogue: '对白', format: '格式', comprehensive: '综合' };
  const editableTemplateFields = ['name', 'description', 'type', 'structure', 'required_fields', 'optional_fields', 'variables', 'style_guidance', 'format_rules', 'prompt_guidance', 'enabled'];
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
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
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
    document.querySelectorAll('.qwen-test-link').forEach(link => { link.hidden = !state.account.is_admin; });
    wallet();
  }
  function creativeSupported() { return state.config?.capabilities?.creative_resources === true || Array.isArray(state.config?.styles); }
  function unavailableCreativeSelection() { return !creativeSupported() && (state.styles.size > 0 || state.exampleRefs.size > 0 || Object.values(state.variables).some(value => String(value).trim()) || state.conflictsConfirmed); }
  function requestBody() {
    const body = { subproject_id: $('scriptSubproject').value, generation_type: $('scriptType').value, template_id: $('scriptTemplate').value, input_request: $('scriptRequest').value.trim(), selection_mode: $('scriptModel').value === 'recommended' ? 'recommended' : 'manual' };
    if (body.selection_mode === 'manual') { body.model_id = $('scriptModel').value; body.thinking_level = $('scriptThinking').value; }
    for (const [key, id] of Object.entries(optionalFields)) if ($(id).value.trim()) body[key] = $(id).value.trim();
    if (state.parent) body.parent_version_id = state.parent.version_id;
    else delete body.change_reason;
    const template = templates().find(item => item.template_id === body.template_id);
    if (creativeSupported()) {
      if (template?.version) body.template_version = template.version;
      body.style_ids = [...state.styles];
      body.example_refs = [...state.exampleRefs.values()].map(item => ({ example_id: item.example_id, version: item.version, reference_roles: [...item.reference_roles] }));
      body.variables = { ...state.variables };
      body.conflicts_confirmed = state.conflictsConfirmed;
    }
    const storyboard = window.IWStoryboard?.options('script'); if (storyboard) body.storyboard_options = storyboard;
    return body;
  }
  window.IWStoryboard?.mountScript({ api, getContext: () => ({ subproject_id: $('scriptSubproject').value, workspace_id: state.account?.workspace_id, account_id: state.account?.account_id }), invalidate: () => invalidateQuote(), anchor: $('scriptModelHelp'), prefix: 'script' });
  function readyToQuote() {
    return Boolean(state.config && $('scriptSubproject').value && $('scriptTemplate').value && $('scriptRequest').value.trim() && (!state.parent || $('scriptChangeReason').value.trim()));
  }
  function localEstimate() {
    return window.IWScriptPricing.estimate(state.config, $('scriptModel').value, $('scriptThinking').value, state.account?.credits, $('scriptType').value);
  }
  function showLocalPrice() {
    const estimate = localEstimate();
    if (estimate) {
      $('scriptPrice').textContent = window.IWScriptPricing.priceText(estimate.price) + '/条';
      $('scriptQuoteDetail').textContent = window.IWScriptPricing.summary(estimate);
    } else {
      $('scriptPrice').textContent = '自动推荐';
      $('scriptQuoteDetail').textContent = '填写需求后自动显示推荐模型、思考强度和积分。';
    }
    wallet();
  }
  function updateControls() {
    for (const item of $('scriptForm').querySelectorAll('input, textarea, select, button')) item.disabled = state.busy;
    for (const item of $('scriptExamples').querySelectorAll('[data-requires-reference]')) item.disabled = state.busy || !state.exampleRefs.has(item.dataset.requiresReference);
    $('scriptMainProject').disabled = state.busy || !state.projects.length;
    $('scriptSubproject').disabled = state.busy || !$('scriptSubproject').options.length;
    $('scriptWorkspace').disabled = state.busy;
    $('scriptNew').disabled = state.busy;
    $('scriptThinking').disabled = state.busy || $('scriptModel').value === 'recommended';
    $('scriptType').disabled = state.busy || Boolean(state.parent);
    let input; try { input = requestBody(); } catch (_) { input = null; }
    const current = input && state.quoteBody === JSON.stringify(input) ? state.quote : null;
    const estimate = localEstimate();
    const needsQuote = !estimate || Boolean(input?.storyboard_options);
    const canGenerate = estimate ? estimate.can_generate && (!current || current.can_generate) : current?.can_generate;
    $('scriptGenerate').disabled = state.busy || !input || !readyToQuote() || unavailableCreativeSelection() || !canGenerate || (needsQuote && !current?.can_generate);
    $('scriptContinue').disabled = $('scriptGenerate').disabled;
    $('scriptForm').setAttribute('aria-busy', String(state.busy));
  }
  function invalidateQuote(schedule = true, resetConflicts = true) {
    if (resetConflicts) state.conflictsConfirmed = false;
    state.quoteSequence += 1;
    state.quote = null; state.quoteBody = '';
    clearTimeout(state.timer);
    showLocalPrice();
    $('scriptWarnings').hidden = true;
    $('scriptConflicts').hidden = true;
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
      for (const level of model.thinking_levels || []) option(select, level.id, (level.display_name || level.id) + ' · ' + window.IWScriptPricing.priceText(level.price), model.available === false || !Number.isInteger(level.price));
      if ([...select.options].some(item => item.value === previous)) select.value = previous;
      $('scriptModelHelp').textContent = model.available === false ? model.unavailable_reason || '当前模型暂不可用' : '将使用你选择的模型和思考强度。';
    }
    updateControls();
  }
  function renderModels() {
    const previous = $('scriptModel').value;
    const effort = $('scriptThinking').value;
    $('scriptModel').replaceChildren(); option($('scriptModel'), 'recommended', '系统推荐');
    for (const model of models()) option($('scriptModel'), model.model_id, model.display_name + (model.available === false ? ' · 当前模型不可用' : ''), model.available === false);
    if ([...$('scriptModel').options].some(item => item.value === previous)) $('scriptModel').value = previous;
    renderThinking(effort);
  }
  function templateInfo() {
    const template = templates().find(item => item.template_id === $('scriptTemplate').value);
    $('scriptTemplateInfo').hidden = !template;
    $('scriptTemplateDescription').textContent = template?.description || '';
    const requirements = template?.required_fields || template?.input_requirements || [];
    const requirementNames = requirements.map(item => {
      const key = typeof item === 'string' ? item : item.field || item.id || '';
      return (typeof item === 'object' && (item.label || item.display_name)) || template?.input_requirement_labels?.[key] || fieldNames[key] || key;
    });
    $('scriptTemplateRequirements').textContent = requirements.length ? '建议提供：' + requirementNames.join('、') : '';
    const examples = template?.example_refs || [];
    $('scriptTemplateExamples').textContent = examples.length ? '已关联 ' + examples.length + ' 份案例，仅供结构、格式与风格参考。' : '当前模板没有关联案例。';
    $('scriptTemplateStructure').textContent = template?.structure?.length ? '结构：' + template.structure.map(item => typeof item === 'string' ? item : item.section + (item.purpose ? '（' + item.purpose + '）' : '')).join(' → ') : '';
    $('scriptVariables').replaceChildren(); state.variables = {};
    for (const variable of template?.variables || []) {
      const wrap = node('div'); const input = node('input'); input.id = 'scriptVariable_' + String(variable.name); input.value = variable.default ?? ''; input.maxLength = 2000;
      const label = node('label', 'field-label', variable.label || variable.name); label.htmlFor = input.id;
      state.variables[variable.name] = input.value;
      input.addEventListener('input', () => { state.variables[variable.name] = input.value; });
      wrap.append(label, input); $('scriptVariables').append(wrap);
    }
  }
  function renderTemplates(preferred = '') {
    const type = $('scriptType').value;
    const previous = preferred || $('scriptTemplate').value;
    $('scriptTemplate').replaceChildren();
    option($('scriptTemplate'), 'none', creativeSupported() ? '自由生成 · 不使用模板' : '自由生成 · 当前脚本服务尚未支持', !creativeSupported());
    for (const origin of ['system', 'personal']) {
      const group = node('optgroup'); group.label = origin === 'system' ? '系统模板' : '我的模板';
      for (const item of templates().filter(value => (value.type || value.generation_type) === type && value.enabled !== false && (value.origin === 'personal' || value.template_id.startsWith('personal_') ? 'personal' : 'system') === origin)) option(group, item.template_id, item.name + (item.version ? ' · v' + item.version : ''));
      if (group.children.length) $('scriptTemplate').append(group);
    }
    if (!previous && $('scriptTemplate').options.length > 1) $('scriptTemplate').selectedIndex = 1;
    if ([...$('scriptTemplate').options].some(item => item.value === previous && !item.disabled)) $('scriptTemplate').value = previous;
    $('scriptTypeHelp').textContent = type === 'screenplay' ? '剧本侧重人物、场景、动作、对白、冲突与转折，适合短剧、故事短片和微电影。' : '脚本侧重主题、创意、视觉事件、旁白与节奏，适合短视频、广告和品牌内容。';
    templateInfo();
  }
  async function requestQuote(explicit) {
    clearTimeout(state.timer);
    if (unavailableCreativeSelection()) { const message = '当前脚本服务尚未提供风格、案例和模板变量能力；请取消相关选择后报价，或等待该服务功能就绪。'; $('scriptQuoteDetail').textContent = message; if (explicit) notice(message, true, 'quote'); return null; }
    if (!readyToQuote()) { if (explicit) $('scriptForm').reportValidity(); return null; }
    const sequence = ++state.quoteSequence;
    const body = requestBody(); const signature = JSON.stringify(body);
    if (!localEstimate()) $('scriptPrice').textContent = '正在自动推荐';
    try {
      const value = await api('/api/scripts/quote', { method: 'POST', body: JSON.stringify(body) });
      if (sequence !== state.quoteSequence || signature !== JSON.stringify(requestBody())) return null;
      state.quote = value; state.quoteBody = signature;
      window.IWStoryboard?.showQuote('script', value.storyboard_quote);
      if (state.noticeSource === 'quote') notice('');
      $('scriptPrice').textContent = window.IWScriptPricing.priceText(value.price) + '/条';
      const choice = (value.display_name || value.model_id || '') + ' · ' + levelName(value.model_id, value.thinking_level);
      $('scriptQuoteDetail').textContent = choice + ' · 本次 1 条：' + window.IWScriptPricing.priceText(value.price) + (value.storyboard_quote ? ' · 分镜：' + window.IWScriptPricing.priceText(value.storyboard_quote.price) : '') + (value.shortfall > 0 ? '。积分不足，本次需要 ' + (value.total_estimated_credit ?? value.price) + ' 积分，当前剩余 ' + value.remaining_credit + ' 积分。' : '');
      $('scriptBalance').textContent = value.remaining_credit === null ? '当前余额：无限积分' : '当前余额：' + value.remaining_credit + ' 积分';
      const warnings = Array.isArray(value.warnings) ? value.warnings : [];
      $('scriptWarnings').hidden = !warnings.length;
      $('scriptWarningText').textContent = warnings.join('\n');
      const conflicts = Array.isArray(value.conflicts) ? value.conflicts : [];
      $('scriptConflicts').hidden = !conflicts.length;
      $('scriptConflictText').textContent = '参考规则存在冲突：\n' + conflicts.map(readable).join('\n') + (state.conflictsConfirmed ? '\n你已明确确认保留当前创作要求。' : '\n请审核后明确确认，确认本身不会提交生成。');
      $('scriptConfirmConflicts').hidden = state.conflictsConfirmed;
      updateControls(); return value;
    } catch (error) {
      if (sequence !== state.quoteSequence) return null;
      state.quote = null; state.quoteBody = '';
      showLocalPrice();
      if (!localEstimate() || window.IWStoryboard?.options('script')) $('scriptQuoteDetail').textContent = error.message;
      updateControls();
      if (explicit) notice(error.message, true, 'quote');
      return null;
    }
  }
  function resetResult() {
    state.detail = null; state.detailSequence += 1; window.IWStoryboard?.scriptDetail(null);
    $('scriptResultTitle').textContent = '等待你的第一份作品';
    $('scriptResultStatus').textContent = '未选择'; $('scriptResultStatus').className = 'status-pill status-neutral';
    $('scriptResultMeta').textContent = '生成的 TXT 会保存在当前项目与子项目中。';
    $('scriptText').textContent = '这里将显示脚本或剧本正文。';
    for (const id of ['scriptTaskError', 'scriptResultActions', 'scriptVersionsPanel', 'scriptRecoveryActions', 'scriptValidationPanel']) $(id).hidden = true;
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
    window.IWStoryboard?.scriptDetail(detail);
    $('scriptResultTitle').textContent = typeName(detail.generation_type) + (detail.output_version ? ' · v' + detail.output_version : '');
    $('scriptResultStatus').textContent = labels[detail.status] || detail.status;
    $('scriptResultStatus').className = 'status-pill status-' + (detail.status === 'completed' ? 'approved' : active.has(detail.status) ? 'generating' : detail.status);
    $('scriptResultMeta').textContent = [detail.source === 'manual_edit' ? '手动修改 · 不扣积分' : [detail.model || detail.model_id, levelName(detail.model_id, detail.thinking_level), (detail.price ?? 0) + ' 积分'].filter(Boolean).join(' · '), date(detail.completed_at || detail.created_at)].filter(Boolean).join(' ｜ ');
    $('scriptTaskError').hidden = !detail.error; $('scriptTaskError').textContent = detail.error || '';
    $('scriptText').textContent = detail.text || (active.has(detail.status) ? '任务已保存，正文完成后会在这里显示。' : '此任务暂无可下载正文。');
    const report = detail.validation_report;
    $('scriptValidationPanel').hidden = !report;
    if (report) {
      const checks = [['file_exists', '正文文件'], ['artifact_registered', '作品归档'], ['version_registered', '版本登记']].map(([key, title]) => title + '：' + (report[key] === true ? '已核验' : report[key] === false ? '未通过' : '未报告'));
      $('scriptValidationSummary').textContent = '基础检查：' + (report.structure_status === 'needs_review' ? '有待人工核对项' : report.structure_status === 'basic_checks_passed' ? '已通过' : '已完成') + '。' + checks.join('；') + '。';
      $('scriptValidationWarnings').textContent = (report.warnings || []).map(readable).join('\n') || '未发现字段缺项或明显截断疑点。';
      $('scriptValidationDetails').textContent = JSON.stringify(report, null, 2);
    }
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
    state.selectionSequence += 1; clearCreative(); state.tasks = []; renderHistory(); resetResult(); clearParent(); invalidateQuote();
    const project = state.projects.find(item => (item.main_project_id || item.project_id) === $('scriptMainProject').value);
    const sub = project?.subprojects?.find(item => item.subproject_id === $('scriptSubproject').value);
    $('scriptLocation').textContent = sub ? project.name + ' / ' + sub.name : '请选择子项目';
    $('scriptNoProject').hidden = Boolean(sub);
    if (state.account) { remember(storageKey('main'), $('scriptMainProject').value); remember(storageKey('sub'), sub?.subproject_id || ''); }
    loadHistory().catch(error => notice(error.message, true));
    loadExamples().catch(error => notice(error.message, true));
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
    state.busy = true; clearTimeout(state.timer); updateControls(); notice('');
    try {
      const body = requestBody();
      let current = state.quoteBody === JSON.stringify(body) ? state.quote : null;
      if (body.selection_mode !== 'manual' || body.storyboard_options) {
        if (!current) current = await requestQuote(true);
        if (!current?.can_generate) return;
      } else if (!localEstimate()?.can_generate) return;
      if (current && !current.can_generate) return;
      if (current?.warnings?.length && !warningsConfirmed) { $('scriptWarnings').hidden = false; return; }
      const requestId = await submissionId(body);
      const result = await api('/api/scripts/tasks', { method: 'POST', body: JSON.stringify({ ...body, request_id: requestId,
        ...(current?.quote_signature ? {quote_signature: current.quote_signature} : {}), warnings_confirmed: warningsConfirmed,
        ...(body.storyboard_options ? { storyboard_confirmed: true } : {}) }) });
      state.pending = null; remember(storageKey('pending'), '');
      invalidateQuote(false);
      // Keep the accepted task visible and pollable if a follow-up read fails.
      state.tasks = [result, ...state.tasks.filter(task => task.task_id !== result.task_id)];
      renderDetail(result);
      await Promise.all([selectTask(result.task_id), refreshAccount()]);
      await loadHistory(result.task_id);
      notice('生成任务已保存。正文完成后可查看、修改和下载 TXT。');
    } catch (error) {
      notice(error.message, true);
      if (error.status === 402 || error.status === 409) { invalidateQuote(false); await requestQuote(false); }
      await refreshAccount().catch(() => {});
    } finally { state.busy = false; updateControls(); }
  }
  function regenerate() {
    const detail = state.detail;
    if (!detail?.version_id || detail.status !== 'completed') return;
    state.parent = detail;
    $('scriptType').value = detail.generation_type; renderTemplates(detail.template_id);
    state.styles = new Set(detail.style_ids || []); renderStyles();
    state.exampleRefs.clear();
    for (const reference of detail.example_refs || []) {
      if (state.examples.some(item => item.example_id === reference.example_id && item.enabled !== false)) state.exampleRefs.set(reference.example_id, { example_id: reference.example_id, version: reference.version, reference_roles: [...reference.reference_roles] });
    }
    renderExamples();
    for (const [name, value] of Object.entries(detail.variables || {})) {
      const input = $('scriptVariable_' + name); if (input) { input.value = value; state.variables[name] = value; }
    }
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
  function readable(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
  function objectJSON(id) {
    let value;
    try { value = JSON.parse($(id).value); } catch (_) { throw new Error('JSON 格式不正确，请检查引号、逗号和括号。'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请输入 JSON 对象。');
    return value;
  }
  function templateJSON() {
    const source = objectJSON('scriptTemplateJSON'); const value = {};
    for (const key of editableTemplateFields) if (Object.hasOwn(source, key)) value[key] = source[key];
    if (typeof value.name !== 'string' || !value.name.trim()) throw new Error('请在 JSON 中填写模板名称 name。');
    if (!Array.isArray(value.structure) || !value.structure.length || !Array.isArray(value.variables)) throw new Error('structure 必须是非空数组，variables 必须是数组（可为空）。');
    return value;
  }
  function creativeContext() { return state.creativeSequence; }
  function sameContext(value) { return value === creativeContext(); }
  function resourceMessage(message, error = false) {
    const dialog = document.querySelector('.scripts-resource-dialog[open]');
    if (!dialog) { notice(message, error); return; }
    let box = dialog.querySelector('.scripts-resource-message');
    if (!box) { box = node('p', 'scripts-resource-message scripts-notice'); box.setAttribute('role', 'status'); dialog.querySelector('.dialog-top').after(box); }
    box.textContent = message; box.hidden = !message; box.classList.toggle('is-error', error);
  }
  function bindAction(id, action) {
    $(id).addEventListener('click', async () => {
      const button = $(id); if (button.disabled || state.busy) return;
      button.disabled = true; const context = creativeContext();
      try { await action(context); }
      catch (error) { if (sameContext(context)) resourceMessage(error.message, true); }
      finally { button.disabled = false; }
    });
  }
  function actionButton(label, action, className = 'text-button') {
    const button = node('button', className, label); button.type = 'button';
    button.addEventListener('click', async () => {
      if (state.busy) return;
      const context = creativeContext(); button.disabled = true;
      try { await action(context); } catch (error) { if (sameContext(context)) resourceMessage(error.message, true); }
      finally { button.disabled = false; }
    }); return button;
  }
  function clearCreative() {
    state.creativeSequence++; state.examplesSequence++; state.recommendationSequence++; state.resourceSequence++;
    state.examples = []; state.exampleRefs.clear(); state.styles.clear(); state.exampleDetail = null; state.templateEdit = null; state.draftId = null;
    $('scriptExamples').replaceChildren(); $('scriptRecommendation').hidden = true; $('scriptExampleFile').value = '';
    for (const id of ['scriptExampleDialog', 'scriptTemplateDialog']) { if ($(id).open) $(id).close(); }
    renderStyles();
  }
  function renderStyles() {
    $('scriptStyles').replaceChildren();
    const styles = (state.config?.styles || []).filter(item => item.enabled !== false);
    const allowed = new Set(styles.map(item => item.style_id));
    state.styles = new Set([...state.styles].filter(id => allowed.has(id)));
    for (const style of styles) {
      const label = node('label', 'scripts-choice'); const check = node('input'); check.type = 'checkbox'; check.checked = state.styles.has(style.style_id);
      check.addEventListener('change', () => { if (check.checked) state.styles.add(style.style_id); else state.styles.delete(style.style_id); invalidateQuote(); });
      const copy = node('span'); copy.append(node('strong', '', style.name), node('small', '', style.description || style.emotional_tone || ''));
      label.append(check, copy); $('scriptStyles').append(label);
    }
    if (!styles.length) $('scriptStyles').append(node('p', 'scripts-help', '暂未配置可选风格，可在语气与风格中填写要求。'));
  }
  function renderExamples() {
    $('scriptExamples').replaceChildren();
    for (const example of state.examples.filter(item => item.enabled !== false)) {
      const row = node('div', 'scripts-resource-row'); const label = node('label', 'scripts-choice'); const check = node('input'); check.type = 'checkbox'; check.checked = state.exampleRefs.has(example.example_id);
      const copy = node('span', '', example.name || example.filename || example.example_id); copy.append(node('small', '', 'v' + example.version + (example.analysis_confirmed ? ' · 已确认分析' : ' · 尚未保存分析')));
      check.addEventListener('change', () => {
        if (check.checked) state.exampleRefs.set(example.example_id, { example_id: example.example_id, version: example.version, reference_roles: [...(example.reference_roles?.length ? example.reference_roles : ['comprehensive'])] });
        else state.exampleRefs.delete(example.example_id);
        renderExamples(); invalidateQuote();
      }); label.append(check, copy); row.append(label);
      const roles = node('div', 'scripts-role-list');
      for (const [role, title] of Object.entries(referenceRoles)) {
        const roleLabel = node('label'); const roleCheck = node('input'); roleCheck.type = 'checkbox'; roleCheck.dataset.requiresReference = example.example_id; roleCheck.checked = state.exampleRefs.get(example.example_id)?.reference_roles.includes(role) || false; roleCheck.disabled = !check.checked;
        roleCheck.addEventListener('change', () => {
          const ref = state.exampleRefs.get(example.example_id); if (!ref) return;
          const next = new Set(ref.reference_roles); if (roleCheck.checked) next.add(role); else next.delete(role);
          if (!next.size) { roleCheck.checked = true; notice('每份参考案例至少保留一种用途。', true); return; }
          ref.reference_roles = [...next]; invalidateQuote();
        }); roleLabel.append(roleCheck, document.createTextNode(title)); roles.append(roleLabel);
      }
      row.append(roles, actionButton('查看 / 分析 / 停用', () => openExample(example.example_id)));
      $('scriptExamples').append(row);
    }
    if (!$('scriptExamples').children.length) $('scriptExamples').append(node('p', 'scripts-help', '暂无案例，上传后可选择并指定用途。'));
  }
  async function loadExamples() {
    if (!creativeSupported()) { state.examples = []; renderExamples(); $('scriptExamples').replaceChildren(node('p', 'scripts-help', '当前脚本服务尚未开启案例资源能力。')); return; }
    const context = creativeContext(); const sequence = ++state.examplesSequence; const subproject = $('scriptSubproject').value;
    if (!subproject) { state.examples = []; renderExamples(); return; }
    const payload = await api('/api/scripts/examples?subproject_id=' + encodeURIComponent(subproject));
    if (!sameContext(context) || sequence !== state.examplesSequence) return;
    state.examples = payload.examples || [];
    for (const [id, ref] of state.exampleRefs) {
      const current = state.examples.find(item => item.example_id === id && item.enabled !== false);
      if (!current) state.exampleRefs.delete(id);
      else ref.version = current.version;
    }
    renderExamples(); invalidateQuote(false);
  }
  async function openExample(id) {
    const context = creativeContext(); const sequence = ++state.resourceSequence;
    const example = await api('/api/scripts/examples/' + encodeURIComponent(id));
    if (!sameContext(context) || sequence !== state.resourceSequence) return;
    state.exampleDetail = example;
    $('scriptExampleMeta').textContent = (example.name || example.filename) + ' · v' + example.version;
    $('scriptExampleText').textContent = example.text || '暂无可预览正文。';
    $('scriptExampleAnalysis').value = JSON.stringify(example.analysis || {}, null, 2);
    $('scriptExampleAnalysisNotice').textContent = '现有已保存分析；可以编辑后明确确认。免费分析仅生成待审核的规则预览。';
    $('scriptExampleDownload').href = '/api/scripts/examples/' + encodeURIComponent(id) + '/download?version=' + example.version + '&workspace_id=' + encodeURIComponent(state.account.workspace_id);
    $('scriptExampleDialog').showModal(); resourceMessage('');
  }
  async function refreshCreativeConfig(preferred = $('scriptTemplate').value) {
    const context = creativeContext(); const config = await api('/api/scripts/config');
    if (!sameContext(context)) return;
    state.config = config; renderTemplates(preferred); renderStyles(); invalidateQuote(false);
  }
  function sourceInfo() {
    const source = $('scriptTemplateSource').value;
    $('scriptTemplateSourceExample').disabled = source !== 'uploaded_file';
    $('scriptTemplateSourceInfo').textContent = source === 'existing_artifact' ? state.detail?.text ? '来源：当前作品 ' + typeName(state.detail.generation_type) + ' v' + (state.detail.output_version || 1) : '请先在作品区选择已完成作品。' : source === 'uploaded_file' ? '请选择已上传并解析的参考案例。' : '生成手动草稿后，编辑结构和默认变量并确认保存。';
  }
  async function loadPersonalTemplates() {
    const context = creativeContext();
    const payload = await api('/api/scripts/templates?include_disabled=true');
    if (!sameContext(context)) return;
    $('scriptPersonalTemplates').replaceChildren();
    for (const template of payload.templates || []) {
      const row = node('div', 'scripts-resource-row'); row.append(node('strong', '', template.name + ' · v' + template.version + (template.enabled === false ? ' · 已停用' : '')));
      row.append(actionButton('预览 / 编辑 / 历史', () => openPersonalTemplate(template.template_id))); $('scriptPersonalTemplates').append(row);
    }
    if (!$('scriptPersonalTemplates').children.length) $('scriptPersonalTemplates').append(node('p', 'scripts-help', '还没有个人模板，可从作品、上传案例或手动草稿创建。'));
  }
  async function openTemplatePanel(source = 'manual') {
    state.resourceSequence++; state.templateEdit = null; state.draftId = null; $('scriptTemplateEditor').hidden = true; $('scriptTemplateSourcePreview').hidden = true;
    $('scriptDraftName').value = ''; $('scriptTemplateSource').value = source;
    $('scriptTemplateSourceExample').replaceChildren();
    for (const example of state.examples.filter(item => item.enabled !== false)) option($('scriptTemplateSourceExample'), example.example_id, example.name || example.filename);
    sourceInfo(); $('scriptTemplateDialog').showModal(); resourceMessage(''); await loadPersonalTemplates();
  }
  function showTemplateEditor(template, draftId = null) {
    state.templateEdit = template; state.draftId = draftId; $('scriptTemplateEditor').hidden = false;
    const editable = {}; for (const key of editableTemplateFields) if (Object.hasOwn(template, key)) editable[key] = template[key];
    editable.variables ||= []; editable.structure ||= [];
    $('scriptTemplateJSON').value = JSON.stringify(editable, null, 2); $('scriptTemplateJSONPreview').textContent = JSON.stringify(editable, null, 2);
    $('scriptTemplateEditorTitle').textContent = draftId ? '待确认的模板草稿' : (template.name || '个人模板') + ' · v' + template.version;
    $('scriptSaveTemplate').textContent = draftId ? '确认并保存模板' : '确认保存为新版本';
    $('scriptCopyTemplate').hidden = Boolean(draftId); $('scriptDisableTemplate').hidden = Boolean(draftId) || template.enabled === false;
    $('scriptTemplateVersions').replaceChildren(); option($('scriptTemplateVersions'), '', '选择历史版本');
    for (const version of template.versions || []) option($('scriptTemplateVersions'), String(version.version), 'v' + version.version + ' · ' + date(version.updated_at || version.created_at));
    $('scriptTemplateVersions').disabled = Boolean(draftId); $('scriptTemplateVersionPreview').hidden = true;
  }
  async function openPersonalTemplate(id) {
    const context = creativeContext(); const sequence = ++state.resourceSequence;
    const template = await api('/api/scripts/templates/' + encodeURIComponent(id));
    if (!sameContext(context) || sequence !== state.resourceSequence) return;
    showTemplateEditor(template); $('scriptTemplateSourcePreview').hidden = true;
  }
  for (const button of document.querySelectorAll('[data-close-dialog]')) button.addEventListener('click', () => { state.resourceSequence++; $(button.dataset.closeDialog).close(); });
  for (const id of ['scriptExampleDialog', 'scriptTemplateDialog']) $(id).addEventListener('close', () => { state.resourceSequence++; });
  bindAction('scriptUploadExample', async context => {
    const file = $('scriptExampleFile').files[0];
    if (!file) throw new Error('请先选择 TXT、DOCX 或 PDF 文件。');
    if (!/\.(txt|docx|pdf)$/i.test(file.name) || file.size > 5 * 1024 * 1024) throw new Error('仅支持最大 5 MiB 的 TXT、DOCX、PDF。');
    if (!$('scriptSubproject').value) throw new Error('请先选择子项目。');
    const body = new FormData(); body.append('file', file); body.append('subproject_id', $('scriptSubproject').value); body.append('generation_type', $('scriptType').value); body.append('reference_roles', JSON.stringify(['comprehensive']));
    await api('/api/scripts/examples', { method: 'POST', body });
    if (!sameContext(context)) return; $('scriptExampleFile').value = ''; await loadExamples(); notice('案例已上传，请选择用途，或查看并分析。');
  });
  bindAction('scriptAnalyzeExample', async context => {
    const example = state.exampleDetail; if (!example) return;
    const sequence = state.resourceSequence;
    const result = await api('/api/scripts/examples/' + encodeURIComponent(example.example_id) + '/analyze', { method: 'POST', body: '{}' });
    if (!sameContext(context) || sequence !== state.resourceSequence) return;
    $('scriptExampleAnalysis').value = JSON.stringify(result.analysis || {}, null, 2);
    $('scriptExampleAnalysisNotice').textContent = '分析方式：' + (result.provider || '免费规则分析') + '。这是待审核结果，确认保存后才生效；没有收费模型调用。';
  });
  bindAction('scriptSaveExampleAnalysis', async context => {
    const example = state.exampleDetail; if (!example) return;
    const sequence = state.resourceSequence;
    const updated = await api('/api/scripts/examples/' + encodeURIComponent(example.example_id), { method: 'PATCH', body: JSON.stringify({ expected_version: example.version, analysis: objectJSON('scriptExampleAnalysis') }) });
    if (!sameContext(context)) return; await loadExamples();
    if (sequence !== state.resourceSequence || !$('scriptExampleDialog').open) return;
    state.exampleDetail = updated; await openExample(example.example_id); resourceMessage('分析已确认保存为新版本。');
  });
  bindAction('scriptDisableExample', async context => {
    const example = state.exampleDetail; if (!example) return;
    const sequence = state.resourceSequence;
    await api('/api/scripts/examples/' + encodeURIComponent(example.example_id), { method: 'PATCH', body: JSON.stringify({ expected_version: example.version, enabled: false }) });
    if (!sameContext(context)) return; state.exampleRefs.delete(example.example_id); if (sequence === state.resourceSequence && $('scriptExampleDialog').open) $('scriptExampleDialog').close(); await loadExamples(); notice('案例已停用，历史仍保留。');
  });
  bindAction('scriptRecommend', async context => {
    const input = $('scriptRequest').value.trim(); if (!input) throw new Error('请先填写创作要求，再请求推荐。');
    const type = $('scriptType').value; const sequence = ++state.recommendationSequence;
    const result = await api('/api/scripts/recommend', { method: 'POST', body: JSON.stringify({ input_request: input, generation_type: type }) });
    if (!sameContext(context) || sequence !== state.recommendationSequence || input !== $('scriptRequest').value.trim() || type !== $('scriptType').value) return;
    $('scriptRecommendation').hidden = false;
    $('scriptRecommendationText').textContent = '推荐方式：' + (result.provider || '规则匹配') + '；匹配分数：' + readable(result.match_score ?? '未提供') + '（' + (result.score_method || '规则评分') + '）\n理由：' + readable(result.reasons || []) + '\n待补充：' + readable(result.missing_information || []);
    $('scriptRecommendationChoices').replaceChildren();
    const template = result.recommended_template; const styles = result.recommended_styles || [];
    $('scriptRecommendationChoices').append(node('p', '', '推荐模板：' + (template?.name || '自由生成') + '；风格：' + (styles.map(item => item.name).join('、') || '无')));
    const stillRelevant = () => { if (!sameContext(context) || input !== $('scriptRequest').value.trim() || type !== $('scriptType').value) throw new Error('创作要求已变化，请重新获取推荐。'); };
    const accept = candidate => { stillRelevant(); const id = candidate?.template_id || 'none'; if (![...$('scriptTemplate').options].some(item => item.value === id)) throw new Error('此模板不适用于当前内容类型或已停用。'); $('scriptTemplate').value = id; state.styles = new Set(styles.map(item => item.style_id)); templateInfo(); renderStyles(); invalidateQuote(); notice('推荐已应用，仍可在模板和风格中手动改选。'); };
    $('scriptRecommendationChoices').append(actionButton('接受推荐', () => accept(template), 'secondary-button'));
    const alternatives = result.alternatives || {};
    const candidateTemplates = Array.isArray(alternatives) ? alternatives : alternatives.templates || [];
    const candidateLabel = item => (item.name || item.template_id || item.style_id) + ' · 匹配 ' + (item.match_score ?? '未报告') + (item.matched_terms?.length ? ' · ' + item.matched_terms.join('、') : ' · 无明确关键词');
    for (const candidate of candidateTemplates) { const item = candidate.template || candidate; $('scriptRecommendationChoices').append(actionButton('改选模板：' + candidateLabel(item), () => accept(item))); }
    for (const item of alternatives.styles || []) $('scriptRecommendationChoices').append(actionButton('添加风格：' + candidateLabel(item), () => { stillRelevant(); state.styles.add(item.style_id); renderStyles(); invalidateQuote(); notice('候选风格已添加，可在叙事风格中取消。'); }));
  });
  bindAction('scriptManageTemplates', () => openTemplatePanel());
  bindAction('scriptSaveAsTemplate', () => openTemplatePanel('existing_artifact'));
  $('scriptTemplateSource').addEventListener('change', sourceInfo);
  bindAction('scriptCreateTemplateDraft', async context => {
    const source = $('scriptTemplateSource').value; const body = { source_type: source, extraction_method: $('scriptExtractionMethod').value, type: $('scriptType').value, name: $('scriptDraftName').value.trim() || undefined };
    if (source === 'existing_artifact') { if (!state.detail?.text || state.detail.status !== 'completed') throw new Error('请先选择已完成作品。'); body.task_id = state.detail.task_id; body.type = state.detail.generation_type; }
    if (source === 'uploaded_file') { body.example_id = $('scriptTemplateSourceExample').value; if (!body.example_id) throw new Error('请先上传并选择案例。'); }
    const sequence = ++state.resourceSequence;
    const result = await api('/api/scripts/template-drafts', { method: 'POST', body: JSON.stringify(body) });
    if (!sameContext(context) || sequence !== state.resourceSequence) return;
    showTemplateEditor(result.template, result.draft_id); $('scriptTemplateDraftNotice').textContent = (result.notice || '请审核规则预览后确认保存。') + ' 分析方式：' + (result.provider || '规则预览') + '；未真实调用收费模型。';
    $('scriptTemplateSourceText').textContent = readable(result.source_preview || '手动草稿，无来源正文。'); $('scriptTemplateSourcePreview').hidden = false;
  });
  bindAction('scriptPreviewTemplateJSON', () => { $('scriptTemplateJSONPreview').textContent = JSON.stringify(templateJSON(), null, 2); });
  bindAction('scriptSaveTemplate', async context => {
    const template = templateJSON(); const current = state.templateEdit; if (!current) return;
    const sequence = state.resourceSequence;
    const result = await api(state.draftId ? '/api/scripts/templates' : '/api/scripts/templates/' + encodeURIComponent(current.template_id), { method: state.draftId ? 'POST' : 'PATCH', body: JSON.stringify(state.draftId ? { draft_id: state.draftId, confirmed: true, template } : { expected_version: current.version, template }) });
    if (!sameContext(context)) return;
    await refreshCreativeConfig(); await loadPersonalTemplates(); if (sequence !== state.resourceSequence || !$('scriptTemplateDialog').open) return; await openPersonalTemplate(result.template_id); resourceMessage('模板已确认保存，历史版本保留。');
  });
  bindAction('scriptCopyTemplate', async context => {
    const current = state.templateEdit; if (!current?.template_id) return;
    const sequence = state.resourceSequence;
    const result = await api('/api/scripts/templates/' + encodeURIComponent(current.template_id) + '/copy', { method: 'POST', body: JSON.stringify({ name: (current.name || '模板') + ' 副本' }) });
    if (!sameContext(context)) return; await refreshCreativeConfig(); await loadPersonalTemplates(); if (sequence !== state.resourceSequence || !$('scriptTemplateDialog').open) return; await openPersonalTemplate(result.template_id); resourceMessage('已创建独立副本。');
  });
  bindAction('scriptDisableTemplate', async context => {
    const current = state.templateEdit; if (!current?.template_id) return;
    const sequence = state.resourceSequence;
    await api('/api/scripts/templates/' + encodeURIComponent(current.template_id), { method: 'PATCH', body: JSON.stringify({ expected_version: current.version, template: { enabled: false } }) });
    if (!sameContext(context)) return; await refreshCreativeConfig(); await loadPersonalTemplates(); if (sequence !== state.resourceSequence || !$('scriptTemplateDialog').open) return; await openPersonalTemplate(current.template_id); resourceMessage('模板已停用，历史版本保留。');
  });
  $('scriptTemplateVersions').addEventListener('change', async () => {
    const current = state.templateEdit; const version = $('scriptTemplateVersions').value; const context = creativeContext(); const sequence = state.resourceSequence;
    $('scriptTemplateVersionPreview').hidden = true; if (!current?.template_id || !version) return;
    try { const result = await api('/api/scripts/templates/' + encodeURIComponent(current.template_id) + '/versions/' + encodeURIComponent(version)); if (!sameContext(context) || sequence !== state.resourceSequence || version !== $('scriptTemplateVersions').value) return; $('scriptTemplateVersionPreview').textContent = JSON.stringify(result, null, 2); $('scriptTemplateVersionPreview').hidden = false; }
    catch (error) { if (sameContext(context)) resourceMessage(error.message, true); }
  });
  bindAction('scriptConfirmConflicts', async () => { state.conflictsConfirmed = true; invalidateQuote(false, false); await requestQuote(true); });
  $('scriptForm').addEventListener('input', () => invalidateQuote());
  $('scriptType').addEventListener('change', () => { $('scriptRecommendation').hidden = true; renderTemplates(); invalidateQuote(); });
  $('scriptRequest').addEventListener('input', () => { $('scriptRecommendation').hidden = true; });
  $('scriptTemplate').addEventListener('change', () => { templateInfo(); invalidateQuote(); });
  $('scriptModel').addEventListener('change', () => { renderThinking(); invalidateQuote(); });
  $('scriptThinking').addEventListener('change', () => invalidateQuote());
  $('scriptForm').addEventListener('submit', event => { event.preventDefault(); generate(false); });
  $('scriptContinue').addEventListener('click', () => generate(true));
  $('scriptModify').addEventListener('click', () => { $('scriptWarnings').hidden = true; $('scriptModel').focus(); });
  $('scriptMainProject').addEventListener('change', () => renderSubprojects());
  $('scriptSubproject').addEventListener('change', changeSubproject);
  $('scriptClearParent').addEventListener('click', () => { clearParent(); invalidateQuote(); });
  $('scriptRegenerate').addEventListener('click', regenerate);
  $('scriptRecover').addEventListener('click', event => recoverOrResume('recover', event.currentTarget));
  $('scriptResume').addEventListener('click', event => recoverOrResume('run', event.currentTarget));
  $('scriptNew').addEventListener('click', () => {
    $('scriptForm').reset(); clearParent(); state.exampleRefs.clear(); state.styles.clear(); renderExamples(); renderStyles(); $('scriptRecommendation').hidden = true; renderTemplates(); renderThinking(); invalidateQuote(false);
    state.pending = null; remember(storageKey('pending'), ''); notice(''); $('scriptRequest').focus();
  });
  $('scriptWorkspace').addEventListener('change', async event => {
    clearCreative(); invalidateQuote(false);
    try { await api('/api/workspaces/select', { method: 'POST', body: JSON.stringify({ workspace_id: event.target.value }) }); location.assign('/scripts'); }
    catch (error) { event.target.value = state.account.workspace_id; await loadExamples().catch(() => {}); notice(error.message, true); }
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
      renderModels(); renderTemplates(); renderStyles(); invalidateQuote(false);
      await Promise.all([loadHistory(), loadExamples()]);
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
    renderModels(); renderTemplates(); renderStyles(); await loadProjects(); wallet(); updateControls();
    const params = new URL(location.href).searchParams, target = params.get('task_id');
    if (target) await loadHistory();
    if (target && state.tasks.some(task => task.task_id === target)) { await selectTask(target); if (state.detail?.status === 'completed') { if (params.get('action') === 'edit') $('scriptEdit').click(); if (params.get('action') === 'template') $('scriptSaveAsTemplate').click(); } }
    setInterval(async () => {
      if (document.hidden || state.busy || state.refreshing || !state.account || (!active.has(state.detail?.status) && !state.tasks.some(item => active.has(item.status) || active.has(item.storyboard_task?.status) || (item.storyboard_options && item.status === 'completed' && !item.storyboard_task)))) return;
      state.refreshing = true;
      try { await loadHistory(); await refreshAccount(); }
      catch (error) { notice(error.message, true); }
      finally { state.refreshing = false; }
    }, 5000);
  })().catch(error => { notice(error.message, true); $('scriptGenerate').disabled = true; });
})();
