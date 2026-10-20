(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const uuid = () => crypto.randomUUID().replaceAll('-', '');
  const requestedWorkspace = new URL(location.href).searchParams.get('workspace_id');
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
    if (!headers.has('X-Workspace-ID') && (state.account?.workspace_id || /^[0-9a-f]{32}$/.test(requestedWorkspace || ''))) headers.set('X-Workspace-ID',state.account?.workspace_id || requestedWorkspace);
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
    if (typeof path !== 'string' || !/^\/api\/(video\/|artifacts\/|scripts\/tasks\/)/.test(path)) return '';
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
  function rateFor(resolution) {
    const rate = state.config?.rates?.[resolution];
    return Number.isInteger(rate) && rate > 0 ? rate : null;
  }
  function resolutionView() {
    const resolution = $('videoResolution').value;
    const rate = rateFor(resolution);
    $('videoFixedPrice').textContent = rate ? `${resolution} · ${rate} 积分 / 秒 · 平台预设价格` : '正在读取预设价格';
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
    $('videoAdminLink').hidden = !state.account.is_admin;
    document.querySelectorAll('.qwen-test-link').forEach(link => { link.hidden = !state.account.is_admin; });
    const previous = $('videoResolution').value;
    $('videoResolution').replaceChildren();
    for (const resolution of enabled) {
      const option = el('option', '', resolution);
      option.value = resolution;
      $('videoResolution').append(option);
    }
    $('videoResolution').value = enabled.includes(previous) ? previous : enabled[0] || '';
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
      if (window.IWAssetTray) figure.append(window.IWAssetTray.imageActions(asset));
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
      const meta = shot.source === 'upload' ? '用户上传 · MP4 · 0 积分' : `${modes[shot.mode] || shot.mode} · ${resolution || '旧版分镜'}${size} · ${shot.duration} 秒 · ${(shot.reference_ids || []).length} 张参考图${cost != null ? ` · ${cost} 积分` : resolution ? ' · 待设置价格' : ''}`;
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
      if (shot.output_url && window.IWAssetTray) actions.append(window.IWAssetTray.videoActions({artifact_id:shot.artifact_id || shot.output?.artifact_id,media_type:'video',shot_id:shot.shot_id,filename:shot.name,url:mediaURL(shot.output_url),download_url:mediaURL(shot.download_url)}));
      if (shot.bound_clip) { const preview=el('button','text-button','预览绑定视频'); preview.type='button'; preview.addEventListener('click',()=>window.IWAssetActions.preview({...shot.bound_clip,media_type:'video',filename:shot.name})); actions.append(preview); }
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
  function projectView(project, force = false, preserveDraft = false) {
    state.project = project;
    $('videoProjectDetail').hidden = false; $('videoCreatePanel').hidden = true;
    $('videoProjectIntro').hidden = false;
    $('videoActiveName').textContent = project.name; $('videoProjectCrumb').textContent = project.name;
    $('videoActiveScript').textContent = project.script || '未填写脚本说明';
    const signature = JSON.stringify([project.assets, project.shots, state.config.rates, enabledResolutions()]);
    if (force || signature !== state.projectSignature) { renderAssets(project); renderShots(project); state.projectSignature = signature; }
    document.dispatchEvent(new CustomEvent('video-project-view', { detail: { project, force, preserveDraft } }));
  }
  async function selectProject(id, preserveDraft = false) {
    if (!preserveDraft) state.draftRevision = (state.draftRevision || 0) + 1;
    state.selectingProject = id;
    const project = await api(`/api/video/projects/${id}`);
    if (state.selectingProject !== id) return;
    if (!preserveDraft) $('videoShotForm').reset(); state.shotRequest = uuid(); state.referenceOrder = [];
    $('videoReferenceChoices').replaceChildren();
    projectView(project, true, preserveDraft); resolutionView(); await loadProjects();
    const url = new URL(location.href); url.searchParams.set('project', id); history.replaceState(null, '', url);
  }
  async function ensureProject() {
    if (state.project) return state.project;
    if (!state.account) throw new Error('工作台正在加载，请稍候再试');
    if (state.projectCreating) return state.projectCreating;
    const revision = state.draftRevision;
    state.projectDraft ||= { request_id: state.projectRequest, name: $('videoProjectName').value.trim() || `新作品 ${new Date().toLocaleString('zh-CN', { hour12: false })}`, script: '' };
    const pending = (async () => {
      const project = await api('/api/video/projects', { method: 'POST', body: JSON.stringify(state.projectDraft) });
      if (state.draftRevision !== revision) throw new Error('已切换项目，请在当前项目重新操作');
      await selectProject(project.project_id, true);
      if (state.draftRevision !== revision || state.project?.project_id !== project.project_id) throw new Error('已切换项目，请在当前项目重新操作');
      state.projectRequest = uuid(); state.projectDraft = null;
      return project;
    })();
    state.projectCreating = pending;
    try { return await pending; } finally { if (state.projectCreating === pending) state.projectCreating = null; }
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

  $('newVideoProject').addEventListener('click', () => {
    state.draftRevision = (state.draftRevision || 0) + 1; state.selectingProject = null; state.project = null; state.projectSignature = '';
    state.projectRequest = uuid(); state.projectDraft = null; state.referenceOrder = []; state.shotRequest = uuid();
    $('videoShotForm').reset(); $('videoProjectName').value = ''; $('videoActiveScript').textContent = ''; $('videoUploadMessage').textContent = '';
    renderAssets({ assets: [] }); renderShots({ shots: [] });
    document.dispatchEvent(new CustomEvent('video-project-cleared')); $('videoCreatePanel').hidden = false; $('videoProjectIntro').hidden = true;
    $('videoProjectCrumb').textContent = '新作品'; notice('');
    const url = new URL(location.href); url.searchParams.delete('project'); history.replaceState(null, '', url);
    loadProjects().catch(error => notice(error.message, true));
  });
  $('videoShotForm').addEventListener('submit', event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const id = (await ensureProject()).project_id;
      const mode = $('videoMode').value;
      const refs = mode === 'T2V' ? [] : [...state.referenceOrder];
      if ((mode === 'I2V' && (refs.length < 1 || refs.length > 2)) || (mode === 'R2V' && (refs.length < 1 || refs.length > 9))) throw new Error(mode === 'I2V' ? '首尾帧模式需要选择 1–2 张参考图' : '参考素材模式需要选择 1–9 张参考图');
      await api(`/api/video/projects/${id}/shots`, { method: 'POST', body: JSON.stringify({ request_id: state.shotRequest, name: $('videoShotName').value.trim(), prompt: $('videoShotPrompt').value.trim(), mode, duration: Number($('videoDuration').value), resolution: $('videoResolution').value, aspect_ratio: $('videoRatio').value, reference_ids: refs }) });
      state.shotRequest = uuid(); $('videoShotName').value = ''; $('videoShotPrompt').value = '';
      if (state.project?.project_id === id) await refresh(); notice('分镜已保存。检查内容和积分后，点击对应镜头的“生成视频”。');
    });
  });
  $('videoMode').addEventListener('change', updateMode);
  $('videoResolution').addEventListener('change', resolutionView);
  $('videoDuration').addEventListener('input', quote);
  $('videoRefresh').addEventListener('click', event => busy(event.currentTarget, async () => { await refresh(); await loadProjects(); notice('状态已刷新'); }));
  $('videoWorkspace').addEventListener('change', async event => { try { await api('/api/workspaces/select', { method: 'POST', body: JSON.stringify({ workspace_id: event.target.value }) }); location.assign('/video'); } catch (error) { event.target.value = state.account.workspace_id; notice(error.message, true); } });
  $('videoLogout').addEventListener('click', event => busy(event.currentTarget, async () => { await api('/api/auth/logout', { method: 'POST' }); location.assign('/login'); }));

  window.IWVideo = { state, api, refresh, notice, busy, mediaURL, el, uuid, ensureProject, renderAssets, updateMode, selectProject };
  (async () => {
    state.csrf = (await api('/api/config')).csrf_token;
    await refreshAccount();
    for (const workspace of state.account.workspaces) { const option = el('option', '', workspace.name); option.value = workspace.workspace_id; option.selected = workspace.workspace_id === state.account.workspace_id; $('videoWorkspace').append(option); }
    state.config = await api('/api/video/config'); configView();
    const projects = await loadProjects(); const requested = new URL(location.href).searchParams.get('project');
    if (requested && projects.some(project => project.project_id === requested)) await selectProject(requested);
    else if (projects.length) await selectProject(projects[0].project_id);
    document.dispatchEvent(new CustomEvent('video-ready'));
    setInterval(() => { if (!document.hidden && state.account) refresh().catch(error => notice(error.message, true)); }, 8000);
  })().catch(error => notice(error.message, true));
})();
