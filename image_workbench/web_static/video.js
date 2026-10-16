(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const uuid = () => crypto.randomUUID().replaceAll('-', '');
  const state = { account: null, config: null, csrf: '', project: null, projectRequest: uuid(), shotRequest: uuid(), referenceOrder: [], runRequests: new Map(), projectSignature: '', loading: false };
  const labels = { draft: '待生成', queued: '已排队', running: '生成中', generating: '生成中', completed: '已完成', failed: '生成失败', uncertain: '状态待确认' };
  const modes = { T2V: '文生视频', I2V: '首帧 / 首尾帧', R2V: '参考素材' };
  const resolutions = ['720p', '1080p'];
  const ratioNames = { landscape: '横向', portrait: '竖向', square: '方形' };
  const active = new Set(['queued', 'running', 'generating', 'uncertain']);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function notice(text, error = false) {
    const node = $('videoNotice');
    node.hidden = !text;
    node.classList.toggle('is-error', error);
    node.textContent = text;
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.csrf) headers.set('X-Workbench-CSRF', state.csrf);
    if (state.account) headers.set('X-Workspace-ID', state.account.workspace_id);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) { location.assign('/login'); throw new Error('请重新登录'); }
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '请求未完成，请稍后重试');
    return payload;
  }
  async function busy(button, action) {
    if (button.disabled) return;
    button.disabled = true;
    try { await action(); } catch (error) { notice(error.message, true); }
    finally { if (button.isConnected) button.disabled = false; }
  }
  function mediaURL(path) {
    if (typeof path !== 'string' || !path.startsWith('/api/video/')) return '';
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin) return '';
    url.searchParams.set('workspace_id', state.account.workspace_id);
    return url.pathname + url.search;
  }
  function balance() {
    const wallet = state.account.credits || {};
    $('videoAccountName').textContent = state.account.username;
    $('videoCredits').textContent = wallet.is_unlimited ? '无限积分' : `可用 ${wallet.remaining_credit ?? 0} 积分 · 冻结 ${wallet.frozen_credit ?? 0}`;
  }
  async function refreshAccount() { state.account = await api('/api/auth/me'); balance(); }
  function enabledResolutions() {
    const enabled = state.config?.enabled_resolutions;
    return resolutions.filter(value => (Array.isArray(enabled) ? enabled : ['720p']).includes(value));
  }
  function pricingRates() {
    const rates = { ...state.config.rates };
    for (const resolution of enabledResolutions()) rates[resolution] = Number($(resolution === '720p' ? 'videoRate720' : 'videoRate1080').value);
    return rates;
  }
  function rateFor(resolution) {
    const rate = state.config?.rates?.[resolution];
    return Number.isInteger(rate) && rate > 0 ? rate : null;
  }
  function resolutionView() {
    const resolution = $('videoResolution').value;
    for (const option of $('videoRatio').options) {
      const size = state.config?.resolutions?.[resolution]?.[option.value];
      option.textContent = `${ratioNames[option.value]}${size ? ` · ${size.width} × ${size.height}` : ''}`;
    }
    quote();
  }
  function quote() {
    const resolution = $('videoResolution').value;
    const rate = rateFor(resolution);
    const duration = Number($('videoDuration').value);
    $('videoQuote').textContent = rate ? `${resolution} · ${duration} 秒 × ${rate} 积分 / 秒 = ${duration * rate} 积分 · 生成时冻结` : `管理员设置 ${resolution} 每秒积分后，即可提交生成`;
  }
  function configView() {
    const c = state.config;
    const enabled = enabledResolutions();
    $('videoWorkerStatus').textContent = c.worker_online ? 'H3 执行器在线' : 'H3 执行器离线 · 新任务排队';
    $('videoPricingPanel').hidden = !state.account.is_admin;
    $('videoAdminLink').hidden = !state.account.is_admin;
    $('videoRateSummary').textContent = enabled.map(resolution => `${resolution}：${rateFor(resolution) ? `${rateFor(resolution)} 积分 / 秒` : '尚未设置'}`).join(' · ');
    for (const resolution of resolutions) {
      const input = $(resolution === '720p' ? 'videoRate720' : 'videoRate1080');
      input.disabled = !enabled.includes(resolution);
      $(resolution === '720p' ? 'videoRate720Label' : 'videoRate1080Label').hidden = input.disabled;
      input.hidden = input.disabled;
      if (document.activeElement !== input) input.value = rateFor(resolution) ?? '';
    }
    const previous = $('videoResolution').value;
    $('videoResolution').replaceChildren();
    for (const resolution of enabled) {
      const rate = rateFor(resolution);
      const option = el('option', '', `${resolution}${rate ? ` · ${rate} 积分 / 秒` : ' · 待设置价格'}`);
      option.value = resolution;
      $('videoResolution').append(option);
    }
    $('videoResolution').value = enabled.includes(previous) ? previous : enabled[0] || '';
    if (!c.configured && state.account.is_admin) $('videoPricingPanel').open = true;
    resolutionView();
  }
  async function loadProjects() {
    const result = await api('/api/video/projects');
    $('videoProjectList').replaceChildren();
    for (const project of result.projects) {
      const button = el('button', 'video-project-button', project.name);
      button.type = 'button';
      button.classList.toggle('is-active', project.project_id === state.project?.project_id);
      button.addEventListener('click', () => selectProject(project.project_id).catch(error => notice(error.message, true)));
      $('videoProjectList').append(button);
    }
    if (!result.projects.length) $('videoProjectList').append(el('p', 'field-hint', '还没有视频项目'));
    return result.projects;
  }
  function renderAssets(project) {
    state.referenceOrder = state.referenceOrder.filter(id => project.assets.some(asset => asset.asset_id === id));
    const selected = new Set(state.referenceOrder);
    $('videoAssetCount').textContent = `${project.assets.length} 张`;
    $('videoAssetGrid').replaceChildren();
    $('videoReferenceChoices').replaceChildren();
    for (const [index, asset] of project.assets.entries()) {
      const figure = el('figure', 'video-asset');
      const image = el('img'); image.src = mediaURL(asset.url); image.alt = asset.filename; image.loading = 'lazy';
      figure.append(image, el('figcaption', '', `${index + 1}. ${asset.filename}`));
      $('videoAssetGrid').append(figure);
      const label = el('label', 'video-reference-choice');
      const input = el('input'); input.type = 'checkbox'; input.value = asset.asset_id; input.checked = selected.has(asset.asset_id);
      input.addEventListener('change', () => {
        state.referenceOrder = state.referenceOrder.filter(id => id !== asset.asset_id);
        if (input.checked) state.referenceOrder.push(asset.asset_id);
        referenceLabels();
      });
      const thumb = image.cloneNode(); thumb.alt = '';
      label.append(input, thumb, el('span', '', asset.filename), el('b', 'video-reference-order', ''));
      $('videoReferenceChoices').append(label);
    }
    if (!project.assets.length) $('videoAssetGrid').append(el('p', 'field-hint', '先添加角色、场景或首帧图片。'));
    updateMode();
  }
  function updateMode() {
    const mode = $('videoMode').value;
    $('videoReferenceHelp').textContent = mode === 'T2V' ? '文生视频仅使用本镜头的文字描述。' : mode === 'I2V' ? '先勾选首帧，再勾选可选的末帧。取消后重新勾选可调整顺序。' : '选择 1–9 张参考图；按勾选顺序对应 Picture 1、Picture 2…取消后重新勾选可调整顺序。';
    for (const input of $('videoReferenceChoices').querySelectorAll('input')) input.disabled = mode === 'T2V';
    referenceLabels();
  }
  function referenceLabels() {
    const mode = $('videoMode').value;
    for (const label of $('videoReferenceChoices').querySelectorAll('label')) {
      const index = state.referenceOrder.indexOf(label.querySelector('input').value);
      label.querySelector('.video-reference-order').textContent = mode === 'T2V' || index < 0 ? '' : mode === 'I2V' && index < 2 ? ['首帧', '末帧'][index] : `Picture ${index + 1}`;
    }
  }
  function renderShots(project) {
    const existingVideos = new Map(Array.from($('videoShots').querySelectorAll('video'), video => [video.dataset.jobId, video]));
    $('videoShots').replaceChildren();
    $('videoShotCount').textContent = `${project.shots.length} 个分镜`;
    for (const [index, shot] of project.shots.entries()) {
      const job = shot.current_job;
      const status = shot.status || job?.status || 'draft';
      const card = el('article', 'video-shot-card');
      const copy = el('div', 'video-shot-copy');
      const heading = el('div', 'video-shot-heading');
      heading.append(el('h3', '', `${String(index + 1).padStart(2, '0')}  ${shot.name}`), el('span', `status-pill video-status-${status}`, labels[status] || status));
      const resolution = resolutions.includes(shot.resolution) ? shot.resolution : null;
      const requiresRebuild = shot.requires_rebuild === true || !resolution;
      const blockedReason = shot.run_blocked_reason || (resolution && !enabledResolutions().includes(resolution) ? `${resolution} 暂未开放，请新建 720p 分镜。` : '');
      const rate = rateFor(resolution);
      const expected = requiresRebuild ? null : shot.estimated_credit ?? (rate ? shot.duration * rate : null);
      const cost = job?.credit_cost ?? expected;
      const size = Number.isInteger(shot.width) && Number.isInteger(shot.height) ? ` · ${shot.width} × ${shot.height}` : '';
      const meta = `${modes[shot.mode] || shot.mode} · ${resolution || '旧版分镜'}${size} · ${shot.duration} 秒 · ${(shot.reference_ids || []).length} 张参考图${cost != null ? ` · ${cost} 积分` : resolution ? ' · 待设置价格' : ''}`;
      copy.append(heading, el('p', 'video-shot-meta', meta), el('p', 'video-shot-prompt', shot.prompt));
      if (status === 'failed' || status === 'uncertain') copy.append(el('p', 'field-hint', status === 'uncertain' ? '原任务结果尚未确认，积分仍冻结。请联系管理员核对执行器；不会自动重复生成。' : `本次未完成，冻结积分已退回。${requiresRebuild || blockedReason ? '' : '可检查素材后重新提交。'}`));
      const actions = el('div', 'video-shot-actions');
      if (status === 'draft' || status === 'failed') {
        if (requiresRebuild || blockedReason) {
          actions.append(el('p', 'field-hint', blockedReason || `此分镜使用旧版尺寸。请在上方按原内容重新创建 ${enabledResolutions().join(' 或 ')} 高清分镜。`));
        } else {
          const canGenerate = rate !== null && expected !== null;
          const button = el('button', 'primary-button', canGenerate ? `${status === 'failed' ? '重新生成' : '生成视频'} · ${expected} 积分` : `等待设置 ${resolution} 价格`);
          button.type = 'button'; button.disabled = !canGenerate;
          button.addEventListener('click', () => busy(button, async () => {
            const key = `${shot.shot_id}:${job?.job_id || 'first'}`;
            if (!state.runRequests.has(key)) state.runRequests.set(key, uuid());
            const body = { request_id: state.runRequests.get(key), confirm_generation: true, expected_credit: expected };
            if (status === 'failed') body.retry_of = job.job_id;
            await api(`/api/video/projects/${project.project_id}/shots/${shot.shot_id}/run`, { method: 'POST', body: JSON.stringify(body) });
            notice(`镜头已排队，已冻结 ${expected} 积分。${state.config.worker_online ? '' : '执行器上线后会继续处理。'}`);
            await refresh();
          }));
          actions.append(button);
        }
      }
      if (shot.download_url) {
        const link = el('a', 'secondary-button', '下载 MP4'); link.href = mediaURL(shot.download_url); actions.append(link);
      }
      if (status === 'queued') actions.append(el('span', 'field-hint', '等待本机算力 · 不会重复提交'));
      copy.append(actions);
      const output = el('div', 'video-shot-output');
      if (shot.output_url) {
        const key = job?.job_id || shot.shot_id;
        let video = existingVideos.get(key);
        if (!video) { video = el('video'); video.controls = true; video.preload = 'metadata'; video.src = mediaURL(shot.output_url); video.dataset.jobId = key; video.setAttribute('playsinline', ''); video.setAttribute('aria-label', `${shot.name} 生成视频`); }
        output.append(video);
      } else output.append(el('span', '', status === 'draft' ? '视频将在这里呈现' : status === 'queued' ? '已进入生成队列' : status === 'failed' ? '本次没有可交付视频' : status === 'uncertain' ? '等待核对原任务结果' : '正在生成这一镜'));
      card.append(copy, output); $('videoShots').append(card);
    }
    if (!project.shots.length) $('videoShots').append(el('div', 'video-empty', '还没有分镜。填写上方镜头内容并保存，再单独点击生成。'));
  }
  function projectView(project, force = false) {
    state.project = project;
    $('videoProjectDetail').hidden = false; $('videoCreatePanel').hidden = true;
    $('videoActiveName').textContent = project.name; $('videoProjectCrumb').textContent = project.name;
    $('videoActiveScript').textContent = project.script || '未填写脚本说明';
    const signature = JSON.stringify([project.assets, project.shots, state.config.rates, enabledResolutions()]);
    if (force || signature !== state.projectSignature) { renderAssets(project); renderShots(project); state.projectSignature = signature; }
  }
  async function selectProject(id) {
    const project = await api(`/api/video/projects/${id}`);
    $('videoShotForm').reset(); state.shotRequest = uuid(); state.referenceOrder = [];
    $('videoReferenceChoices').replaceChildren();
    projectView(project, true); resolutionView(); await loadProjects();
    const url = new URL(location.href); url.searchParams.set('project', id); history.replaceState(null, '', url);
  }
  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    try {
      state.config = await api('/api/video/config'); configView();
      await refreshAccount();
      if (state.project) {
        const id = state.project.project_id;
        const project = await api(`/api/video/projects/${id}`);
        if (state.project?.project_id === id) projectView(project);
      }
    } finally { state.loading = false; }
  }

  $('videoProjectForm').addEventListener('submit', event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const project = await api('/api/video/projects', { method: 'POST', body: JSON.stringify({ request_id: state.projectRequest, name: $('videoProjectName').value.trim(), script: $('videoScript').value }) });
      state.projectRequest = uuid(); $('videoProjectForm').reset(); await selectProject(project.project_id); notice('项目已保存。添加参考资产，然后创建分镜。');
    });
  });
  $('newVideoProject').addEventListener('click', () => { state.project = null; state.projectSignature = ''; $('videoCreatePanel').hidden = false; $('videoProjectDetail').hidden = true; $('videoProjectCrumb').textContent = '新建项目'; const url = new URL(location.href); url.searchParams.delete('project'); history.replaceState(null, '', url); $('videoProjectName').focus(); loadProjects().catch(error => notice(error.message, true)); });
  $('videoAssets').addEventListener('change', async event => {
    const input = event.target; const files = Array.from(input.files); const id = state.project?.project_id;
    if (!files.length || !id) return;
    input.disabled = true;
    try {
      if (files.some(file => file.size > 20 * 1024 * 1024) || files.reduce((sum, file) => sum + file.size, 0) > 40 * 1024 * 1024) throw new Error('单张图片最大 20 MB，一次上传合计最大 40 MB');
      const form = new FormData(); files.forEach(file => form.append('files', file));
      $('videoUploadMessage').textContent = '正在上传并校验图片…';
      await api(`/api/video/projects/${id}/assets`, { method: 'POST', body: form });
      if (state.project?.project_id === id) await refresh();
      $('videoUploadMessage').textContent = '参考资产已保存'; notice('参考资产已保存，可在分镜中选择。');
    } catch (error) { $('videoUploadMessage').textContent = '上传未完成，可重新选择图片重试'; notice(error.message, true); }
    finally { input.disabled = false; input.value = ''; }
  });
  $('videoShotForm').addEventListener('submit', event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const id = state.project?.project_id; if (!id) return;
      const mode = $('videoMode').value;
      const refs = mode === 'T2V' ? [] : [...state.referenceOrder];
      if ((mode === 'I2V' && (refs.length < 1 || refs.length > 2)) || (mode === 'R2V' && (refs.length < 1 || refs.length > 9))) throw new Error(mode === 'I2V' ? '首尾帧模式需要选择 1–2 张参考图' : '参考素材模式需要选择 1–9 张参考图');
      await api(`/api/video/projects/${id}/shots`, { method: 'POST', body: JSON.stringify({ request_id: state.shotRequest, name: $('videoShotName').value.trim(), prompt: $('videoShotPrompt').value.trim(), mode, duration: Number($('videoDuration').value), resolution: $('videoResolution').value, aspect_ratio: $('videoRatio').value, reference_ids: refs }) });
      state.shotRequest = uuid(); $('videoShotName').value = ''; $('videoShotPrompt').value = '';
      if (state.project?.project_id === id) await refresh(); notice('分镜已保存。检查内容和积分后，点击对应镜头的“生成视频”。');
    });
  });
  $('videoPricingForm').addEventListener('submit', event => { event.preventDefault(); busy(event.submitter, async () => { await api('/api/video/config', { method: 'PUT', body: JSON.stringify({ rates: pricingRates() }) }); await refresh(); notice('每秒积分已保存。已提交镜头继续按原价格结算。'); }); });
  $('videoMode').addEventListener('change', updateMode);
  $('videoResolution').addEventListener('change', resolutionView);
  $('videoDuration').addEventListener('input', quote);
  $('videoRefresh').addEventListener('click', event => busy(event.currentTarget, async () => { await refresh(); await loadProjects(); notice('状态已刷新'); }));
  $('videoWorkspace').addEventListener('change', async event => { try { await api('/api/workspaces/select', { method: 'POST', body: JSON.stringify({ workspace_id: event.target.value }) }); location.assign('/video'); } catch (error) { event.target.value = state.account.workspace_id; notice(error.message, true); } });
  $('videoLogout').addEventListener('click', event => busy(event.currentTarget, async () => { await api('/api/auth/logout', { method: 'POST' }); location.assign('/login'); }));

  (async () => {
    state.csrf = (await api('/api/config')).csrf_token;
    await refreshAccount();
    for (const workspace of state.account.workspaces) { const option = el('option', '', workspace.name); option.value = workspace.workspace_id; option.selected = workspace.workspace_id === state.account.workspace_id; $('videoWorkspace').append(option); }
    state.config = await api('/api/video/config'); configView();
    const projects = await loadProjects(); const requested = new URL(location.href).searchParams.get('project');
    if (requested && projects.some(project => project.project_id === requested)) await selectProject(requested);
    else if (projects.length) await selectProject(projects[0].project_id);
    if (!state.config.configured) notice(state.account.is_admin ? '请展开“视频积分设置”，填写每秒积分。设置前可以保存项目、资产和分镜。' : '视频价格尚未设置。你可以先保存项目、资产和分镜。');
    setInterval(() => { if (!document.hidden && state.account) refresh().catch(error => notice(error.message, true)); }, 8000);
  })().catch(error => notice(error.message, true));
})();
