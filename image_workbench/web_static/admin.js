(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const viewNames = { overview: '运行概览', releases: '版本与部署', reports: '功能测试报告', configuration: '运行配置' };
  const state = { csrf: '', overview: null, reports: [], operations: [], active: null, requesting: false, loading: false, online: false, preview: null, pollTimer: 0, toastTimer: 0, detailRequest: 0, stopped: false, controllers: new Set() };
  const successStatuses = new Set(['succeeded', 'success', 'passed', 'completed', 'ready']);
  const failureStatuses = new Set(['failed', 'error', 'cancelled', 'canceled', 'rejected']);
  const extraStatusLabels = { needs_attention: '需核实', acknowledged: '已核实结束', stale: '源码已变更' };
  const backendLabels = { codex: 'Codex 生图', local: '本机演练' };
  const executionLabels = { local: '本机执行', remote: '远程队列' };
  const statusLabels = { succeeded: '已完成', success: '已完成', passed: '通过', completed: '已完成', ready: '已就绪', failed: '失败', error: '失败', cancelled: '已取消', canceled: '已取消', rejected: '未通过', queued: '等待中', pending: '等待中', running: '进行中', in_progress: '进行中', checking: '检查中', preparing: '准备中', deploying: '部署中', skipped: '跳过', warning: '需关注', partial: '部分通过' };
  const policyLabels = { mode: '模式', enabled: '是否启用', allow_registration: '允许注册', registration_enabled: '开放注册', invitation_required: '需要邀请码', require_invitation: '需要邀请码', require_email: '需要邮箱', require_activation: '需要激活', activation_required: '需要激活', max_accounts: '账号上限', max_users: '账号上限', max_workspaces: '工作空间上限', max_projects: '项目上限', max_pending: '等待任务上限', max_pending_per_account: '每账号等待上限', max_concurrent: '并发上限', max_concurrent_jobs: '并发任务上限', max_active_jobs: '活动任务上限', max_active_per_user: '每账号活动任务上限', max_queued_jobs: '排队任务上限', max_queue_size: '队列上限', max_per_account: '每账号上限', max_per_workspace: '每工作空间上限', total_slots: '总执行名额', available_slots: '可用名额', reserved_slots: '预留名额', initial_credits: '初始积分', signup_credits: '注册赠送积分', default_credits: '默认积分', generation_cost: '生成图片积分', edit_cost: '修改图片积分', task_cost: '每任务积分', cost_per_task: '每任务积分', cost_per_image: '每张图片积分', credits_per_task: '每任务积分', welcome_credits: '新账号积分', daily_limit: '每日上限', global_limit: '全局上限', account_limit: '账号上限', workspace_limit: '工作空间上限', backend: '生成后端', execution_mode: '执行方式', worker_running: '执行器运行中', worker_enabled: '执行器已启用', version: '服务版本', loaded_at: '服务启动', email_required: '需要邮箱', invitation_configured: '已配置邀请码', activation_configured: '已配置激活', public_origin: '公开服务地址' };

  function element(tag, className = '', content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }
  function write(id, value) { $(id).textContent = value === undefined || value === null || value === '' ? '—' : String(value); }
  function number(value) { return value !== undefined && value !== null && Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN') : '—'; }
  function date(value) {
    if (!value) return '时间未记录';
    const parsed = new Date(typeof value === 'number' && value < 100000000000 ? value * 1000 : value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  }
  function summary(value) {
    if (value === undefined || value === null) return '';
    if (typeof value === 'object') return JSON.stringify(value, null, 2);
    return String(value);
  }
  function short(value, limit = 20) { const text = String(value || '—'); return text.length > limit ? `${text.slice(0, limit)}…` : text; }
  function status(value) { return String(value || '').toLowerCase(); }
  function successful(item) { return successStatuses.has(status(item?.status)); }
  function failed(item) { return failureStatuses.has(status(item?.status)) || Number(item?.failed) > 0; }
  function terminal(item) { return !item || successful(item) || failureStatuses.has(status(item.status)) || ['needs_attention', 'acknowledged'].includes(status(item.status)); }
  function badge(value, label) {
    const key = status(value);
    const tone = successStatuses.has(key) ? 'success' : failureStatuses.has(key) ? 'error' : ['queued', 'pending', 'running', 'in_progress', 'checking', 'preparing', 'deploying', 'warning', 'partial', 'needs_attention', 'stale'].includes(key) ? 'warning' : 'neutral';
    return element('span', `badge badge-${tone}`, label || extraStatusLabels[key] || statusLabels[key] || value || '未记录');
  }
  function setBadge(id, tone, text) { $(id).className = `badge badge-${tone}`; write(id, text); }
  function operationName(operation) {
    const kind = String(operation?.kind || '');
    if (/deploy/.test(kind)) return '版本部署';
    if (/prepare|release|package/.test(kind)) return '准备更新包';
    if (/check|test/.test(kind)) return '功能检查';
    return operation?.title || '维护操作';
  }
  function currentReport(report) {
    if (typeof report?.current_source === 'boolean') return report.current_source;
    const fingerprint = state.overview?.version?.source_fingerprint;
    return report?.source_fingerprint && fingerprint ? report.source_fingerprint === fingerprint : null;
  }
  function latestReport() { return state.reports[0] || state.overview?.latest_report || null; }
  function reportTotals(report) {
    return { total: report?.total, passed: report?.passed, failed: report?.failed, skipped: report?.skipped };
  }
  function reportStatus(report) {
    if (!report) return '尚未检查';
    if (failed(report)) return '检查失败';
    return successful(report) ? '检查通过' : extraStatusLabels[status(report.status)] || statusLabels[status(report.status)] || report.status || '状态未记录';
  }
  function duration(value) { return Number.isFinite(Number(value)) && value !== null && value !== undefined ? `${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 秒` : '—'; }
  function addDetail(list, label, value, className = '') {
    const row = element('div');
    row.append(element('dt', '', label), element('dd', className, value === null || value === undefined || value === '' ? '—' : value));
    list.append(row);
  }
  function displayValue(value) {
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (value === null || value === undefined) return '未设置';
    if (typeof value === 'object') return Array.isArray(value) ? value.map(displayValue).join('、') || '无' : Object.entries(value).map(([key, item]) => `${policyLabels[key] || key}：${displayValue(item)}`).join('\n') || '无';
    return String(value);
  }
  function errorMessage(error) { return error?.message || '操作未完成，请刷新后重试。'; }
  function showError(message) { write('errorText', message); $('errorBanner').hidden = !message; }
  function toast(message) {
    window.clearTimeout(state.toastTimer);
    write('toast', message); $('toast').hidden = false;
    state.toastTimer = window.setTimeout(() => { $('toast').hidden = true; }, 5500);
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    state.controllers.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    const headers = new Headers({ Accept: 'application/json' });
    if (options.method === 'POST') { headers.set('Content-Type', 'application/json'); headers.set('X-Workbench-CSRF', state.csrf); }
    try {
      const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options, headers, signal: controller.signal });
      if (response.status === 401) { location.replace('/login'); throw new Error('登录已过期，请重新登录。'); }
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        if (response.status === 403) state.online = false;
        const detail = typeof payload?.detail === 'string' ? payload.detail : payload?.detail?.message || payload?.message;
        throw new Error(detail || (response.status === 403 ? '当前账号没有管理权限。' : `请求失败（${response.status}），请重试。`));
      }
      if (!payload || typeof payload !== 'object') throw new Error('服务返回了无法识别的数据，请刷新重试。');
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时，请刷新数据确认操作状态。');
      if (error instanceof TypeError) throw new Error('无法连接工作台，请确认服务仍在运行后重试。');
      throw error;
    } finally { window.clearTimeout(timeout); state.controllers.delete(controller); }
  }

  function selectView(name, focus = false) {
    const selected = viewNames[name] ? name : 'overview';
    document.querySelectorAll('.admin-view').forEach(view => { view.hidden = view.id !== `view-${selected}`; });
    document.querySelectorAll('.admin-nav [data-view]').forEach(link => {
      const active = link.dataset.view === selected;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    write('breadcrumbLabel', viewNames[selected]);
    document.title = `${viewNames[selected]} · Image Workbench 管理控制台`;
    if (focus) $(`view-${selected}`).querySelector('h1').focus({ preventScroll: true });
  }

  function renderOverview() {
    const overview = state.overview;
    if (!overview) return;
    const { version = {}, service = {}, counts = {}, deployment = {}, policy = {} } = overview;
    write('adminName', overview.username || '管理员');
    write('heroVersion', short(version.id, 22)); $('heroVersion').title = version.id || '';
    write('heroLoaded', `启动于 ${date(version.loaded_at)}`);
    const running = typeof service.worker_running === 'boolean' ? service.worker_running : null;
    write('serviceHeadline', running === true ? '工作台正在运行' : running === false ? '服务在线，执行器未运行' : '服务已连接');
    write('serviceDescription', running === true ? '执行器已连接，可在下方查看当前任务队列与检查结果。' : running === false ? '管理服务可访问；任务执行情况需结合执行方式与队列查看。' : '已读取当前服务信息，执行器状态暂未提供。');
    write('backendTag', backendLabels[service.backend] || '后端信息未提供');
    write('executionTag', executionLabels[service.execution_mode] || '执行方式未提供');
    setBadge('workerStatus', running === true ? 'success' : running === false ? 'warning' : 'neutral', running === true ? '执行器运行中' : running === false ? '执行器未运行' : '执行器状态未知');
    [['accountCount', 'accounts'], ['workspaceCount', 'workspaces'], ['projectCount', 'projects'], ['reservationCount', 'reservations']].forEach(([id, key]) => write(id, number(counts[key])));
    const queue = service.job_counts || {};
    $('queueCounts').replaceChildren();
    const queueLabels = { queued: '排队中', pending: '等待中', running: '执行中', completed: '已完成', succeeded: '已完成', failed: '失败', uncertain: '结果待确认', cancelled: '已取消', draft: '草稿', submitted: '已提交' };
    if (Object.keys(queue).length) {
      Object.entries(queue).forEach(([key, count]) => { const item = element('div', `queue-item${key === 'running' ? ' running' : ''}`); item.append(element('span', '', queueLabels[key] || key), element('strong', '', number(count))); $('queueCounts').append(item); });
    } else $('queueCounts').append(element('p', 'empty-inline', '当前没有任务排队'));
    write('sourceVersion', version.id); write('sourceFingerprint', version.source_fingerprint); write('sourceLoaded', date(version.loaded_at));
    setBadge('versionMode', 'neutral', version.mode === 'release' ? '发布版本' : '本机源码版本');
    write('deploymentTarget', deployment.target || '尚未配置部署目标');
    const available = deployment.available === true;
    setBadge('deploymentState', available ? 'success' : 'warning', available ? '已配置' : '暂不可用');
    write('deploymentReason', deployment.reason || (available ? '目标服务已配置，可准备更新包并预览部署信息。' : '服务端尚未启用部署。配置部署目标与执行方式后即可使用。'));
    renderPolicy('registrationPolicy', policy.registration);
    renderPolicy('capacityPolicy', policy.capacity);
    renderPolicy('schedulingPolicy', policy.scheduling);
    const scheduling = policy.scheduling;
    write('schedulingSummary', scheduling ? `执行名额 ${scheduling.active} / ${scheduling.max_active_total}，剩余 ${scheduling.available_slots}；结果待确认的任务继续占用名额。` : '队列数据来自当前服务，刷新可查看最新状态。');
    write('schedulingNote', service.execution_mode === 'remote' ? '每个远程执行器一次处理一项任务；并行需要多个执行器和独立缓存。每账号上限指网站账号。剩余名额不代表在线执行器数量。' : '本地执行器当前按顺序运行，有效并发上限为 1。配置值仅在远程执行模式下允许提高。');
    renderPolicy('creditsPolicy', policy.credits);
    renderPolicy('servicePolicy', { backend: backendLabels[service.backend] || service.backend, execution_mode: executionLabels[service.execution_mode] || service.execution_mode, worker_running: service.worker_running, version: version.id, loaded_at: date(version.loaded_at) });
  }

  function renderPolicy(id, policy) {
    const list = $(id); list.replaceChildren();
    if (policy === undefined || policy === null) { addDetail(list, '配置状态', '服务未提供此项配置'); return; }
    if (typeof policy !== 'object') { addDetail(list, '当前设置', displayValue(policy)); return; }
    const entries = Object.entries(policy);
    if (!entries.length) addDetail(list, '配置状态', '未设置额外规则');
    const labels = { initial_credit: '新账号赠送积分', max_pending_total: '全站待处理任务上限', max_pending_per_account: '每账号待处理任务上限', max_daily_submitted_credit: '每日提交积分预算（UTC）', generate: '纯需求生成 / 张', reference_or_edit: '参考图或图片修改 / 张', configured_max_active_total: '配置的全站并发上限', max_active_total: '当前有效并发上限', max_active_per_account: '每网站账号并发上限', queued: '等待领取', running: '已领取', uncertain: '结果待确认', active: '已占用执行名额', available_slots: '剩余执行名额', eligible_queued: '符合当前名额条件的候选任务' };
    entries.forEach(([key, value]) => {
      let shown = displayValue(value);
      if (key.startsWith('max_') && (value === 0 || value === null)) shown = '未设上限';
      if (key === 'mode') shown = { open: '开放注册', invite: '凭一次性邀请码注册', closed: '暂停注册' }[value] || shown;
      addDetail(list, labels[key] || policyLabels[key] || key, shown);
    });
  }

  function renderLatestReport() {
    const report = latestReport();
    const container = $('latestReport'); container.replaceChildren();
    const totals = reportTotals(report);
    if (!report) { container.append(element('p', 'empty-inline', '尚未运行功能检查。完成首次检查后，这里会显示结果。')); }
    else {
      const heading = element('div', 'latest-report-heading');
      heading.append(element('strong', '', reportStatus(report)), badge(failed(report) ? 'failed' : report.status));
      container.append(heading, element('p', 'report-time', `${date(report.created_at)} · ${duration(report.duration_seconds)}`));
      const meter = element('div', 'result-meter'); meter.setAttribute('aria-hidden', 'true');
      const total = Math.max(Number(totals.total) || 0, (Number(totals.passed) || 0) + (Number(totals.failed) || 0) + (Number(totals.skipped) || 0));
      if (total > 0) ['passed', 'failed', 'skipped'].forEach(key => { const part = element('span', `meter-${key}`); part.style.width = `${Math.max(0, Math.min(100, (Number(totals[key]) || 0) / total * 100))}%`; meter.append(part); });
      container.append(meter);
      const breakdown = element('div', 'result-breakdown');
      [['passed', '通过'], ['failed', '失败'], ['skipped', '跳过']].forEach(([key, label]) => { const item = element('span'); item.append(element('strong', '', number(totals[key])), document.createTextNode(label)); breakdown.append(item); });
      container.append(breakdown);
      if (currentReport(report) === false) container.append(element('p', 'stale-note', '源码已有变化，请重新运行检查。'));
    }
    const cards = $('reportSummary'); cards.replaceChildren();
    [['最近检查', reportStatus(report)], ['通过', number(totals.passed)], ['失败', number(totals.failed)], ['跳过', number(totals.skipped)]].forEach(([label, value]) => { const card = element('article'); card.append(element('span', '', label), element('strong', '', value)); cards.append(card); });
  }

  function renderReports() {
    const filter = $('reportFilter').value;
    const reports = state.reports.filter(report => filter === 'all' || (filter === 'failed' ? failed(report) : successful(report) && !failed(report)));
    const table = $('reportTableBody'); table.replaceChildren();
    $('reportEmpty').hidden = reports.length > 0;
    $('reportEmpty').querySelector('h3').textContent = state.reports.length ? '没有符合筛选条件的报告' : '还没有检查报告';
    $('reportEmpty').querySelector('p').textContent = state.reports.length ? '选择其他状态，查看已有检查记录。' : '点击“运行功能检查”，完成后在这里查看结果。';
    reports.forEach(report => {
      const row = element('tr');
      const name = element('td'); name.append(element('span', 'report-name', report.title || '功能检查报告'), element('span', 'report-date', date(report.created_at)));
      const resultStatus = element('td'); resultStatus.append(badge(failed(report) ? 'failed' : report.status));
      const counts = element('td'); const breakdown = element('div', 'report-result');
      breakdown.append(element('span', '', `${number(report.passed)} 通过`), element('span', Number(report.failed) > 0 ? 'failed-count' : '', `${number(report.failed)} 失败`), element('span', '', `${number(report.skipped)} 跳过`)); counts.append(breakdown);
      const version = element('td'); const matched = currentReport(report); version.append(badge(matched === true ? 'passed' : matched === false ? 'warning' : 'unknown', matched === true ? '当前源码' : matched === false ? '源码已变更' : '未记录指纹'));
      const action = element('td'); const button = element('button', 'activity-detail', '查看'); button.type = 'button'; button.setAttribute('aria-label', `查看${report.title || '功能检查报告'}，${date(report.created_at)}`); button.addEventListener('click', () => showReport(report)); action.append(button);
      row.append(name, resultStatus, counts, element('td', '', duration(report.duration_seconds)), version, action); table.append(row);
    });
  }

  function renderOperations() {
    function fill(id, operations) {
      const container = $(id); container.replaceChildren();
      if (!operations.length) { container.append(element('p', 'empty-inline', '暂无维护记录。运行检查或准备更新包后，会在这里留下记录。')); return; }
      operations.forEach(operation => {
        const row = element('div', 'activity-item');
        const main = element('div'); main.append(element('h3', '', operationName(operation)), element('p', '', short(summary(operation.summary) || operation.target || `记录 ${operation.id}`, 120)));
        const end = element('div', 'activity-item-end'); end.append(element('span', 'activity-time', date(operation.created_at)), badge(operation.status));
        const button = element('button', 'activity-detail', '详情'); button.type = 'button'; button.setAttribute('aria-label', `查看${operationName(operation)}详情`); button.addEventListener('click', () => showOperation(operation)); end.append(button);
        row.append(element('span', 'activity-icon', /deploy/.test(String(operation.kind)) ? '↗' : /prepare|release/.test(String(operation.kind)) ? '↑' : '✓'), main, end); container.append(row);
      });
    }
    fill('recentOperations', state.operations.slice(0, 4)); fill('operationHistory', state.operations);
    write('operationCount', `${state.operations.length} 条记录`);
  }

  function preparedRelease() {
    const deployment = state.overview?.deployment || {};
    const direct = deployment.prepared_release || deployment.release;
    const operation = state.operations.find(item => item.release_id && successful(item) && /prepare|release|package/.test(String(item.kind)));
    const id = direct?.id || direct?.release_id || deployment.release_id || operation?.release_id;
    if (!id) return null;
    const source = direct?.source_fingerprint;
    if (source && state.overview?.version?.source_fingerprint && source !== state.overview.version.source_fingerprint) return null;
    const reportId = direct?.report_id || operation?.report_id;
    const report = state.reports.find(item => item.id === reportId);
    if (report && (failed(report) || currentReport(report) === false)) return null;
    return { id, reportId };
  }

  function renderControls() {
    const attention = status(state.active?.status) === 'needs_attention';
    const active = state.active && (!terminal(state.active) || attention);
    const busy = state.requesting || state.loading || Boolean(active);
    const canWrite = state.online && Boolean(state.csrf);
    document.querySelectorAll('[data-action="checks"]').forEach(button => { button.disabled = !canWrite || busy; button.textContent = active && /check|test/.test(String(state.active.kind)) ? '功能检查进行中…' : '运行功能检查 ↗'; });
    $('prepareReleaseButton').disabled = !canWrite || busy || state.overview?.deployment?.package_available === false;
    $('prepareReleaseButton').textContent = active && /prepare|release|package/.test(String(state.active.kind)) ? '正在检查与打包…' : '准备更新包';
    const release = preparedRelease();
    const deployment = state.overview?.deployment || {};
    $('deployButton').disabled = !canWrite || busy || !release || deployment.available !== true;
    $('deployButton').title = deployment.available !== true ? deployment.reason || '请先配置部署目标' : !release ? '请先准备通过检查的更新包' : '';
    $('refreshButton').disabled = state.loading || state.requesting;
    $('retryButton').disabled = state.loading;
    write('packageState', release ? `已准备 · ${short(release.id, 26)}` : deployment.package_available === false ? deployment.package_reason || '当前环境不支持准备发布包' : '尚无与当前源码匹配的更新包');
    write('prepareHint', release ? `更新包 ${short(release.id, 20)} 已就绪，可以预览部署信息。` : deployment.package_available === false ? deployment.package_reason || '当前环境无法打包，请检查服务端发布配置。' : '准备成功后，可预览本次部署的版本与目标。');
    $('operationBanner').hidden = !active;
    if (active) { write('operationTitle', `${operationName(state.active)}${attention ? '需核实' : '进行中'}`); write('operationSummary', summary(state.active.summary) || '后台正在执行，完成后会自动更新报告与维护记录。'); $('operationBanner').querySelector('.operation-live').textContent = attention ? '在维护记录中处理' : '自动更新'; }
    $('confirmDeployButton').disabled = state.requesting || !state.preview;
    $('cancelDeployButton').disabled = state.requesting;
    $('deployDialog').querySelector('.close-button').disabled = state.requesting;
  }

  function render() { renderOverview(); renderLatestReport(); renderReports(); renderOperations(); renderControls(); }
  function sorted(items) { return Array.isArray(items) ? items.filter(item => item && typeof item === 'object').sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)) : []; }
  function setConnection(connected) { const node = $('connectionStatus'); node.className = `connection ${connected ? 'online' : 'offline'}`; node.replaceChildren(element('i'), document.createTextNode(connected ? '服务已连接' : '连接需检查')); }

  async function loadData({ silent = false } = {}) {
    if (state.loading || state.stopped) return;
    state.loading = true; renderControls();
    try {
      const routes = [['权限', '/api/auth/config'], ['概览', '/api/admin/overview'], ['报告', '/api/admin/reports'], ['维护记录', '/api/admin/operations']];
      const results = await Promise.allSettled(routes.map(([, path]) => api(path)));
      if (state.stopped) return;
      const errors = results.flatMap((result, index) => result.status === 'rejected' ? [`${routes[index][0]}：${errorMessage(result.reason)}`] : []);
      if (results[0].status === 'fulfilled') state.csrf = results[0].value.csrf_token || '';
      state.online = results[0].status === 'fulfilled' && results[1].status === 'fulfilled';
      if (results[1].status === 'fulfilled') {
        state.overview = results[1].value;
        state.active = state.overview.active_operation || null;
      }
      if (results[2].status === 'fulfilled') state.reports = sorted(results[2].value.reports);
      if (results[3].status === 'fulfilled') {
        state.operations = sorted(results[3].value.operations);
        if (!state.active) state.active = state.operations.find(item => !terminal(item)) || null;
      }
      setConnection(state.online);
      showError(errors.join('；'));
      if (state.online) write('lastUpdated', `最近同步 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}${errors.length ? ' · 部分数据读取失败' : ''}`);
      if (!silent && !errors.length) toast('管理数据已更新');
      render();
    } finally { state.loading = false; renderControls(); schedulePoll(); }
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    if (state.stopped || document.hidden || !state.active || terminal(state.active)) return;
    state.pollTimer = window.setTimeout(pollOperation, 3000);
  }
  async function pollOperation() {
    if (state.stopped || document.hidden || state.loading || state.requesting) { schedulePoll(); return; }
    const id = state.active?.id;
    if (!id) return;
    try {
      const payload = await api(`/api/admin/operations/${encodeURIComponent(id)}`);
      if (state.stopped) return;
      state.active = payload.operation || payload;
      if (terminal(state.active)) {
        const done = state.active; state.active = null;
        toast(`${operationName(done)}${successful(done) ? '已完成' : '未完成'}，可查看维护记录。`);
        await loadData({ silent: true });
      } else { showError(''); renderControls(); }
    } catch (error) { if (!state.stopped) showError(`${errorMessage(error)} 后台任务可能仍在执行，请勿重复提交。`); }
    finally { schedulePoll(); }
  }

  async function startOperation(path, body = {}) {
    if (state.requesting || !terminal(state.active)) return;
    state.requesting = true; showError(''); renderControls();
    try {
      const payload = await api(path, { method: 'POST', body: JSON.stringify(body) });
      const operation = payload.operation || payload;
      if (!operation.id) throw new Error('服务未返回操作编号，请刷新维护记录确认状态。');
      state.active = operation;
      state.operations = [operation, ...state.operations.filter(item => item.id !== operation.id)];
      render(); toast(`${operationName(operation)}已提交，进度会自动更新。`);
      if (terminal(operation)) await loadData({ silent: true });
      return operation;
    } catch (error) {
      showError(errorMessage(error));
      await loadData({ silent: true });
      showError(errorMessage(error));
      return null;
    } finally { state.requesting = false; renderControls(); schedulePoll(); }
  }

  async function previewDeployment() {
    const release = preparedRelease();
    if (!release || state.requesting || !terminal(state.active)) return;
    state.requesting = true; state.preview = null; showError(''); renderControls();
    try {
      const preview = await api('/api/admin/deployments/preview', { method: 'POST', body: JSON.stringify({ release_id: release.id }) });
      if (!preview.token || !preview.target) throw new Error('未获得有效部署预览，请检查部署目标配置。');
      state.preview = preview;
      $('deploymentPreview').replaceChildren();
      addDetail($('deploymentPreview'), '部署目标', preview.target);
      addDetail($('deploymentPreview'), '发布版本', preview.release_id || release.id, 'mono');
      if (preview.source_fingerprint) addDetail($('deploymentPreview'), '源码指纹', preview.source_fingerprint, 'mono');
      if (preview.expires_at) addDetail($('deploymentPreview'), '确认有效期', date(preview.expires_at));
      write('previewSummary', summary(preview.summary) || '部署状态会记录在维护记录中。');
      $('deploymentError').hidden = true;
      $('deployDialog').showModal();
    } catch (error) { showError(errorMessage(error)); }
    finally { state.requesting = false; renderControls(); }
  }

  async function confirmDeployment() {
    if (!state.preview || state.requesting) return;
    const preview = state.preview;
    if (preview.expires_at) {
      const expiry = new Date(typeof preview.expires_at === 'number' && preview.expires_at < 100000000000 ? preview.expires_at * 1000 : preview.expires_at).getTime();
      if (Number.isFinite(expiry) && expiry <= Date.now()) { state.preview = null; write('deploymentError', '预览已过期。请关闭此窗口并重新预览部署。'); $('deploymentError').hidden = false; renderControls(); return; }
    }
    const operation = await startOperation('/api/admin/deployments', { token: preview.token, confirm: true });
    if (operation) { state.preview = null; $('deployDialog').close(); }
    else { state.preview = null; write('deploymentError', '部署提交未能确认。请关闭窗口并刷新维护记录；确认没有进行中的部署后，再重新预览。'); $('deploymentError').hidden = false; }
    renderControls();
  }

  function showReport(report) {
    state.detailRequest += 1;
    write('detailEyebrow', 'REPORT DETAILS'); write('detailDialogTitle', report.title || '功能检查报告');
    const content = $('detailContent'); content.replaceChildren(); const list = element('dl', 'detail-list');
    addDetail(list, '检查状态', reportStatus(report)); addDetail(list, '运行时间', date(report.created_at));
    addDetail(list, '检查数量', `总计 ${number(report.total)} · 通过 ${number(report.passed)} · 失败 ${number(report.failed)} · 跳过 ${number(report.skipped)}`);
    addDetail(list, '执行耗时', duration(report.duration_seconds)); addDetail(list, '源码指纹', report.source_fingerprint, 'mono');
    const current = currentReport(report); addDetail(list, '版本匹配', current === true ? '与当前源码一致' : current === false ? '源码已有变化，需要重新检查' : '未提供版本匹配信息');
    content.append(list);
    if (report.summary) content.append(element('p', 'detail-summary', summary(report.summary)));
    const checks = report.checks || report.results;
    if (Array.isArray(checks) && checks.length) {
      const checkList = element('ul', 'check-list');
      checks.forEach(check => { const item = element('li'); const copy = element('div'); copy.append(element('strong', '', check.title || check.name || check.id || '检查项')); const detail = check.summary || check.message || check.error; if (detail) copy.append(element('p', '', summary(detail))); item.append(copy, badge(check.status)); checkList.append(item); });
      content.append(checkList);
    }
    $('detailDialog').showModal();
  }

  async function showOperation(operation) {
    const request = ++state.detailRequest;
    write('detailEyebrow', 'OPERATION DETAILS'); write('detailDialogTitle', operationName(operation));
    const content = $('detailContent'); content.replaceChildren(element('p', 'empty-inline', '正在读取操作详情…')); $('detailDialog').showModal();
    function draw(item) {
      content.replaceChildren(); const list = element('dl', 'detail-list');
      addDetail(list, '操作编号', item.id, 'mono'); addDetail(list, '执行状态', extraStatusLabels[status(item.status)] || statusLabels[status(item.status)] || item.status);
      addDetail(list, '开始时间', date(item.created_at)); if (item.finished_at) addDetail(list, '完成时间', date(item.finished_at));
      if (item.target) addDetail(list, '部署目标', item.target);
      if (item.release_id) addDetail(list, '更新包', item.release_id, 'mono');
      content.append(list);
      if (item.summary) content.append(element('p', 'detail-summary', summary(item.summary)));
      if (item.error) content.append(element('p', 'inline-error', summary(item.error)));
      const report = state.reports.find(candidate => candidate.id === item.report_id);
      if (report) { const button = element('button', 'button button-secondary', '查看关联测试报告'); button.type = 'button'; button.addEventListener('click', () => { $('detailDialog').close(); showReport(report); }); content.append(button); }
      if (item.can_acknowledge) {
        const recovery = element('div', 'recovery-confirmation');
        const label = element('label'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
        label.append(checkbox, document.createTextNode('我已核实后台进程已停止；涉及部署时已核实目标版本和服务状态。'));
        const button = element('button', 'button button-secondary', '结束待核实记录'); button.type = 'button'; button.disabled = true;
        checkbox.addEventListener('change', () => { button.disabled = !checkbox.checked; });
        button.addEventListener('click', async () => {
          if (!checkbox.checked || state.requesting) return;
          state.requesting = true; button.disabled = true;
          try {
            await api(`/api/admin/operations/${encodeURIComponent(item.id)}/acknowledge`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
            $('detailDialog').close(); await loadData({ silent: true }); toast('待核实记录已结束，未重新执行原操作。');
          } catch (error) { content.append(element('p', 'inline-error', errorMessage(error))); }
          finally { state.requesting = false; button.disabled = !checkbox.checked; renderControls(); }
        });
        recovery.append(label, element('p', 'card-note', '此操作只记录核实结果，不会重试或再次部署。'), button); content.append(recovery);
      }
    }
    try { const payload = await api(`/api/admin/operations/${encodeURIComponent(operation.id)}`); if ($('detailDialog').open && request === state.detailRequest) draw(payload.operation || payload); }
    catch (error) { if ($('detailDialog').open && request === state.detailRequest) { draw(operation); content.append(element('p', 'inline-error', `详情刷新失败，显示已有记录。${errorMessage(error)}`)); } }
  }

  document.querySelectorAll('[data-view]').forEach(link => link.addEventListener('click', event => { event.preventDefault(); const name = link.dataset.view; history.replaceState(null, '', `#${name}`); selectView(name, true); window.scrollTo({ top: 0, behavior: 'auto' }); }));
  document.querySelectorAll('[data-action="checks"]').forEach(button => button.addEventListener('click', () => startOperation('/api/admin/checks')));
  $('prepareReleaseButton').addEventListener('click', () => startOperation('/api/admin/releases/prepare'));
  $('deployButton').addEventListener('click', previewDeployment);
  $('confirmDeployButton').addEventListener('click', confirmDeployment);
  $('refreshButton').addEventListener('click', () => loadData());
  $('retryButton').addEventListener('click', () => loadData());
  $('reportFilter').addEventListener('change', renderReports);
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
  $('deployDialog').addEventListener('cancel', event => { if (state.requesting) event.preventDefault(); });
  $('deployDialog').addEventListener('close', () => { state.preview = null; renderControls(); });
  window.addEventListener('hashchange', () => { if (viewNames[location.hash.slice(1)]) selectView(location.hash.slice(1)); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) window.clearTimeout(state.pollTimer); else if (!state.stopped && !state.requesting) loadData({ silent: true }); });
  window.addEventListener('pagehide', () => { state.stopped = true; window.clearTimeout(state.pollTimer); window.clearTimeout(state.toastTimer); state.controllers.forEach(controller => controller.abort()); });
  window.addEventListener('pageshow', event => { if (event.persisted) { state.stopped = false; loadData({ silent: true }); } });
  selectView(location.hash.slice(1));
  loadData({ silent: true });
})();
