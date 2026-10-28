(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const uuid = () => crypto.randomUUID().replaceAll('-', '');
  const requestedWorkspace = new URL(location.href).searchParams.get('workspace_id');
  const state = { account: null, config: null, csrf: '', project: null, projectRequest: uuid(), shotRequest: uuid(), referenceOrder: [], runRequests: new Map(), projectSignature: '', loading: false, draftRevision: 0, shotRevision: 0, projectReadSequence: 0 };
  const labels = { draft: '待生成', queued: '已排队', running: '生成中', generating: '生成中', completed: '已完成', failed: '生成失败', uncertain: '状态待确认' };
  const modes = { T2V: '文生视频', I2V: '首帧生视频', FL2V: '首尾帧生视频', R2V: '参考素材' };
  const resolutions = ['720p', '1080p'];
  const ratioNames = { landscape: '横向', portrait: '竖向', square: '方形' };
  const active = new Set(['queued', 'running', 'generating', 'uncertain']);

  // Consumers receive detached values and scoped commands. No module can mutate
  // the controller's state through a view or a project-change event.
  function snapshot() {
    return Object.freeze(structuredClone({account: state.account, config: state.config, project: state.project,
      referenceOrder: state.referenceOrder, draftRevision: state.draftRevision, shotRevision: state.shotRevision}));
  }
  function captureContext() {
    return Object.freeze({id: state.project?.project_id ?? null, account: state.account?.account_id,
      workspace: state.account?.workspace_id, revision: state.draftRevision, shotRevision: state.shotRevision});
  }
  function isCurrent(context, id = context.id, checkDraft = false) {
    return !state.selectingProject && !state.switchingWorkspace && context.account === state.account?.account_id
      && context.workspace === state.account?.workspace_id && context.revision === state.draftRevision
      && id === (state.project?.project_id ?? null) && (!checkDraft || context.shotRevision === state.shotRevision);
  }
  function shotChanged() { state.shotRevision++; state.shotRequest = uuid(); }
  function setReferences(ids, {context = captureContext(), mode = $('videoMode').value} = {}) {
    if (!isCurrent(context, context.id, true)) return false;
    if ($('videoBackend').value === 'online-2.0' && (ids.length || mode !== 'T2V')) throw new Error('当前 SD2.0 通道暂不支持参考图，图片仍保留在项目素材库');
    const order = [...new Set(ids)];
    if (!state.project || order.some(id => !state.project.assets.some(asset => asset.asset_id === id))) throw new Error('参考图片不属于当前项目，请刷新后重新选择');
    if (!Object.hasOwn(modes, mode) || order.length > (mode === 'I2V' ? 1 : mode === 'FL2V' ? 2 : 9)) throw new Error('参考图片数量不符合当前模式');
    state.referenceOrder = order; $('videoMode').value = mode; shotChanged(); renderAssets(state.project);
    return true;
  }
  function addReferences(ids, purpose, context) {
    if (!isCurrent(context, context.id, true)) return false;
    const order = [...state.referenceOrder]; let mode = $('videoMode').value;
    if (purpose === 'first' || purpose === 'last') {
      const index = purpose === 'first' ? 0 : 1;
      if (ids.length !== 1 || (order[index] && order[index] !== ids[0])) throw new Error('首尾帧一次只能指定一张；已有图片不会覆盖，请先取消对应选择');
      if (index === 1 && !order[0]) throw new Error('请先选择首帧，再设置尾帧；图片已保存到项目素材库');
      if (order.includes(ids[0]) && order[index] !== ids[0]) throw new Error('同一张图片已用于另一帧，请选择其他素材');
      if (order.length > 2) throw new Error('当前已选多张参考图，请先减少选择再设置首尾帧');
      order[index] = ids[0]; mode = order.length === 2 || mode === 'FL2V' ? 'FL2V' : 'I2V';
    } else {
      for (const id of ids) if (!order.includes(id)) order.push(id);
      const max = mode === 'I2V' ? 1 : mode === 'FL2V' ? 2 : 9;
      if (order.length > max) throw new Error(`当前模式最多选择 ${max} 张图片；图片已保存，请减少选择后重试`);
      if (mode === 'T2V') mode = 'R2V';
    }
    return setReferences(order, {context, mode});
  }

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
    if (!response.ok) throw Object.assign(new Error(typeof payload.detail === 'string' ? payload.detail : '请求未完成，请稍后重试'), {status: response.status});
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
    $('videoFixedPrice').textContent = rate ? `${resolution} · ${rate} 积分 / 秒 · 平台预设价格${$('videoBackend').value === 'online-2.0' ? ' · 在线输出按所选尺寸缩放' : ''}` : '正在读取预设价格';
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
    const h3Option = $('videoBackend').querySelector('option[value="h3"]');
    if (h3Option) {
      h3Option.disabled = c.local_h3_available === false;
      h3Option.hidden = c.local_h3_available === false;
      if (h3Option.disabled && $('videoBackend').value === 'h3') { $('videoBackend').value = 'online-2.0'; updateBackend(); }
    }
    const backend = $('videoBackend').value;
    const online = backend === 'h3' ? c.worker_online : c.workers_online?.[backend] && c.workers_online?.['online-planner'];
    $('videoWorkerStatus').textContent = `${backend === 'h3' ? 'H3' : 'SD2.0'} 执行器${online ? '在线' : '离线 · 新任务排队'}`;
    $('videoAdminLink').hidden = !state.account.is_admin;
    const previous = $('videoResolution').value;
    $('videoResolution').replaceChildren();
    for (const resolution of enabled) {
      const option = el('option', '', resolution);
      option.value = resolution;
      option.disabled = backend === 'online-2.0' && resolution !== '720p';
      $('videoResolution').append(option);
    }
    $('videoResolution').value = enabled.includes(previous) ? previous : enabled[0] || '';
    resolutionView();
  }
  async function loadProjects() {
    const context = captureContext();
    const result = await api('/api/video/projects');
    if (!isCurrent(context)) return result.projects;
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
    $('videoAssetGrid').replaceChildren();
    $('videoReferenceChoices').replaceChildren();
    for (const [index, asset] of project.assets.entries()) {
      const figure = el('figure', 'video-asset');
      const image = el('img'); image.src = mediaURL(asset.url); image.alt = asset.filename; image.loading = 'lazy';
      figure.append(image, el('figcaption', '', `${index + 1}. ${asset.filename}`));
      if (window.IWAssetTray) figure.append(window.IWAssetTray.imageActions(asset));
      $('videoAssetGrid').append(figure);
      if (!selected.has(asset.asset_id)) continue;
      const label = el('div', 'video-reference-choice');
      const input = el('input'); input.type = 'checkbox'; input.value = asset.asset_id; input.checked = true; input.hidden = true;
      input.addEventListener('change', () => {
        state.referenceOrder = state.referenceOrder.filter(id => id !== asset.asset_id);
        if (input.checked) state.referenceOrder.push(asset.asset_id);
        shotChanged();
        referenceLabels();
      });
      const thumb = image.cloneNode(); thumb.alt = '';
      const remove = el('button', 'text-button', '移除'); remove.type = 'button';
      remove.setAttribute('aria-label', `移除参考图 ${asset.filename}`);
      remove.addEventListener('click', event => { event.preventDefault(); state.referenceOrder = state.referenceOrder.filter(id => id !== asset.asset_id); shotChanged(); renderAssets(state.project); });
      label.append(input, thumb, el('span', '', asset.filename), el('b', 'video-reference-order', ''), remove);
      $('videoReferenceChoices').append(label);
    }
    if (!project.assets.length) $('videoAssetGrid').append(el('p', 'field-hint', '先添加角色、场景或首帧图片。'));
    updateMode();
  }
  function updateMode() {
    const mode = $('videoMode').value;
    $('videoReferenceHelp').textContent = mode === 'T2V' ? '文生视频仅使用本镜头的文字描述。' : mode === 'I2V' ? '选择 1 张图片作为首帧，视频从该画面开始。' : mode === 'FL2V' ? '选择 2 张图片：先添加首帧，再添加尾帧。移除后重新添加可调整顺序。' : '选择 1–9 张参考图；按添加顺序对应 Picture 1、Picture 2…移除后重新添加可调整顺序。';
    for (const input of $('videoReferenceChoices').querySelectorAll('input')) input.disabled = mode === 'T2V';
    referenceLabels();
  }
  function updateBackend() {
    if (state.config?.local_h3_available === false) $('videoBackend').value = 'online-2.0';
    const online = $('videoBackend').value === 'online-2.0';
    if (online) {
      $('videoMode').dataset.previous = $('videoMode').value;
      $('videoMode').value = 'T2V';
      $('videoResolution').value = '720p';
      if (![5, 10, 15].includes(Number($('videoDuration').value))) $('videoDuration').value = '5';
    } else if ($('videoMode').dataset.previous) {
      $('videoMode').value = $('videoMode').dataset.previous;
      delete $('videoMode').dataset.previous;
    }
    $('videoMode').disabled = online;
    for (const option of $('videoResolution').options) option.disabled = online && option.value !== '720p';
    $('videoBackendHelp').textContent = online ? 'DeepSeek Flash 按 SD2.0 Skill 整理提示词，再生成视频；支持文生视频，时长 5、10 或 15 秒。参考图暂未开放。' : '本机 H3 支持参考图、首帧和首尾帧。';
    const isOnline = online ? state.config?.workers_online?.['online-2.0'] && state.config?.workers_online?.['online-planner'] : state.config?.worker_online;
    $('videoWorkerStatus').textContent = `${online ? 'SD2.0' : 'H3'} 执行器${isOnline ? '在线' : '离线 · 新任务排队'}`;
    updateMode(); resolutionView(); shotChanged();
  }
  function referenceLabels() {
    const mode = $('videoMode').value;
    for (const label of $('videoReferenceChoices').querySelectorAll('.video-reference-choice')) {
      const index = state.referenceOrder.indexOf(label.querySelector('input').value);
      label.querySelector('.video-reference-order').textContent = mode === 'T2V' || index < 0 ? '' : mode === 'I2V' ? (index === 0 ? '首帧' : '多余参考图') : mode === 'FL2V' ? (['首帧', '尾帧'][index] || '多余参考图') : `Picture ${index + 1}`;
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
      const displayMode = shot.mode === 'I2V' && shot.reference_ids?.length === 2 ? 'FL2V' : shot.mode;
      const meta = shot.source === 'upload' ? '用户上传 · MP4 · 0 积分' : `${shot.backend === 'online-2.0' ? 'SD2.0 · ' : 'H3 · '}${modes[displayMode] || displayMode} · ${resolution || '旧版分镜'}${size} · ${shot.duration} 秒 · ${(shot.reference_ids || []).length} 张参考图${cost != null ? ` · ${cost} 积分` : resolution ? ' · 待设置价格' : ''}`;
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
            const context = captureContext(); if (context.id !== project.project_id || !isCurrent(context)) return;
            const key = `${shot.shot_id}:${job?.job_id || 'first'}`;
            if (!state.runRequests.has(key)) state.runRequests.set(key, uuid());
            const body = { request_id: state.runRequests.get(key), confirm_generation: true, expected_credit: expected };
            if (status === 'failed') body.retry_of = job.job_id;
            await api(`/api/video/projects/${project.project_id}/shots/${shot.shot_id}/run`, { method: 'POST', body: JSON.stringify(body) });
            if (!isCurrent(context)) return;
            const workers = state.config.workers_online;
            const online = shot.backend === 'online-2.0' ? workers?.['online-2.0'] && workers?.['online-planner'] : state.config.worker_online;
            notice(`镜头已排队，已冻结 ${expected} 积分。${online ? '' : '执行器上线后会继续处理。'}`);
            await refresh(context);
          }));
          actions.append(button);
        }
      }
      if (shot.download_url) {
        const link = el('a', 'secondary-button', '下载 MP4'); link.href = mediaURL(shot.download_url); actions.append(link);
      }
      if (status === 'queued') actions.append(el('span', 'field-hint', '等待执行器 · 不会重复提交'));
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
  function projectView(project, force = false, preserveDraft = false, notify = true) {
    state.project = structuredClone(project);
    $('videoProjectDetail').hidden = false; $('videoCreatePanel').hidden = true;
    $('videoProjectIntro').hidden = false;
    $('videoActiveName').textContent = project.name; $('videoProjectCrumb').textContent = project.name;
    $('videoActiveScript').textContent = project.script || '未填写脚本说明';
    const signature = JSON.stringify([project.assets, project.shots, state.config.rates, enabledResolutions()]);
    if (force || signature !== state.projectSignature) { renderAssets(project); renderShots(project); state.projectSignature = signature; }
    if (notify) document.dispatchEvent(new CustomEvent('video-project-view', { detail: { project: structuredClone(project), force, preserveDraft, context: captureContext() } }));
  }
  async function selectProject(id, preserveDraft = false) {
    if (!preserveDraft) state.draftRevision++;
    const selection = {id, workspace: state.account?.workspace_id, revision: state.draftRevision};
    state.selectingProject = selection; state.projectReadSequence++;
    try {
      const project = await api(`/api/video/projects/${id}`);
      if (state.selectingProject !== selection || state.switchingWorkspace || selection.workspace !== state.account?.workspace_id) return;
      state.selectingProject = null;
      if (!preserveDraft) { $('videoShotForm').reset(); updateBackend(); }
      state.referenceOrder = []; $('videoReferenceChoices').replaceChildren();
      projectView(project, true, preserveDraft); resolutionView();
      const url = new URL(location.href); url.searchParams.set('project', id); history.replaceState(null, '', url);
      await loadProjects();
    } finally { if (state.selectingProject === selection) state.selectingProject = null; }
  }
  async function ensureProject() {
    if (state.selectingProject || state.switchingWorkspace) throw new Error('项目正在切换，请稍候再试');
    if (state.project) return structuredClone(state.project);
    if (!state.account) throw new Error('工作台正在加载，请稍候再试');
    if (state.projectCreating && isCurrent(state.projectCreating.context)) return state.projectCreating.promise;
    const context = captureContext();
    state.projectDraft ||= { request_id: state.projectRequest, name: $('videoProjectName').value.trim() || `新作品 ${new Date().toLocaleString('zh-CN', { hour12: false })}`, script: '' };
    const pending = (async () => {
      const project = await api('/api/video/projects', { method: 'POST', body: JSON.stringify(state.projectDraft) });
      if (!isCurrent(context)) throw new Error('已切换项目，请在当前项目重新操作');
      await selectProject(project.project_id, true);
      if (!isCurrent(context, project.project_id)) throw new Error('已切换项目，请在当前项目重新操作');
      state.projectRequest = uuid(); state.projectDraft = null;
      return project;
    })();
    const creating = {context, promise: pending}; state.projectCreating = creating;
    try { return await pending; } finally { if (state.projectCreating === creating) state.projectCreating = null; }
  }
  async function refreshProject(context = captureContext(), {notify = true} = {}) {
    if (!context.id || !isCurrent(context)) return null;
    const sequence = ++state.projectReadSequence;
    let project;
    try { project = await api(`/api/video/projects/${context.id}`, {headers: {'X-Workspace-ID': context.workspace}}); }
    catch (error) {
      if (!isCurrent(context) || sequence !== state.projectReadSequence) return null;
      if (error.status !== 404) throw error;
      clearProject(true);
      const cleared = captureContext();
      await loadProjects();
      if (isCurrent(cleared)) notice('当前项目已删除或移出此工作空间。可在图片工作台回收站恢复项目，或直接上传图片开始新作品。', true);
      return null;
    }
    if (!isCurrent(context) || sequence !== state.projectReadSequence) return null;
    projectView(project, false, false, notify);
    return structuredClone(project);
  }
  async function refresh(context = captureContext()) {
    if (!isCurrent(context) || (state.loading && isCurrent(state.loading))) return;
    state.loading = context;
    try {
      const [config, account] = await Promise.all([api('/api/video/config'), api('/api/auth/me')]);
      if (!isCurrent(context)) return;
      state.config = config; state.account = account; configView(); balance();
      await refreshProject(context);
    } finally { if (state.loading === context) state.loading = false; }
  }

  function clearProject(preserveDraft = false) {
    state.draftRevision = (state.draftRevision || 0) + 1; state.selectingProject = null; state.project = null; state.projectSignature = '';
    state.projectRequest = uuid(); state.projectDraft = null; state.referenceOrder = []; shotChanged();
    if (!preserveDraft) { $('videoShotForm').reset(); updateBackend(); }
    $('videoProjectName').value = ''; $('videoActiveScript').textContent = ''; $('videoUploadMessage').textContent = '';
    renderAssets({ assets: [] }); renderShots({ shots: [] });
    document.dispatchEvent(new CustomEvent('video-project-cleared', {detail: {preserveDraft}})); $('videoCreatePanel').hidden = false; $('videoProjectIntro').hidden = true;
    $('videoProjectCrumb').textContent = '新作品'; notice('');
    const url = new URL(location.href); url.searchParams.delete('project'); history.replaceState(null, '', url);
  }
  $('newVideoProject').addEventListener('click', () => { clearProject(); loadProjects().catch(error => notice(error.message, true)); });
  $('videoShotForm').addEventListener('submit', event => {
    event.preventDefault();
    busy(event.submitter, async () => {
      const id = (await ensureProject()).project_id;
      const context = captureContext();
      const mode = $('videoMode').value;
      const refs = mode === 'T2V' ? [] : [...state.referenceOrder];
      if ($('videoBackend').value === 'online-2.0' && ![5, 10, 15].includes(Number($('videoDuration').value))) throw new Error('SD2.0 的时长请选择 5、10 或 15 秒');
      if (mode === 'I2V' && refs.length !== 1) throw new Error('首帧模式需要选择 1 张参考图');
      if (mode === 'FL2V' && refs.length !== 2) throw new Error('首尾帧模式需要选择 2 张参考图，依次为首帧和尾帧');
      if (mode === 'R2V' && (refs.length < 1 || refs.length > 9)) throw new Error('参考素材模式需要选择 1–9 张参考图');
      await api(`/api/video/projects/${id}/shots`, { method: 'POST', body: JSON.stringify({ request_id: state.shotRequest, name: $('videoShotName').value.trim(), prompt: $('videoShotPrompt').value.trim(), mode: mode === 'FL2V' ? 'I2V' : mode, duration: Number($('videoDuration').value), resolution: $('videoResolution').value, aspect_ratio: $('videoRatio').value, reference_ids: refs, backend: $('videoBackend').value }) });
      if (!isCurrent(context)) return;
      if (isCurrent(context, id, true)) { shotChanged(); $('videoShotName').value = ''; $('videoShotPrompt').value = ''; }
      await refresh(context); notice('分镜已保存。检查内容和积分后，点击对应镜头的“生成视频”。');
    });
  });
  $('videoMode').addEventListener('change', updateMode);
  $('videoBackend').addEventListener('change', updateBackend);
  $('videoShotForm').addEventListener('input', shotChanged);
  $('videoShotForm').addEventListener('change', shotChanged);
  $('videoResolution').addEventListener('change', resolutionView);
  $('videoDuration').addEventListener('input', quote);
  $('videoRefresh').addEventListener('click', event => busy(event.currentTarget, async () => { const context = captureContext(); await refresh(); await loadProjects(); if (isCurrent(context)) notice('状态已刷新'); }));
  $('videoWorkspace').addEventListener('change', async event => { state.draftRevision++; state.switchingWorkspace = true; try { await api('/api/workspaces/select', { method: 'POST', body: JSON.stringify({ workspace_id: event.target.value }) }); location.assign('/video'); } catch (error) { state.switchingWorkspace = false; event.target.value = state.account.workspace_id; notice(error.message, true); } });
  $('videoLogout').addEventListener('click', event => busy(event.currentTarget, async () => { await api('/api/auth/logout', { method: 'POST' }); location.assign('/login'); }));

  window.IWVideo = Object.freeze({ snapshot, captureContext, isCurrent, setReferences, addReferences, refreshProject,
    api, refresh, notice, busy, mediaURL, el, uuid, ensureProject, selectProject });
  (async () => {
    const initialRevision = state.draftRevision;
    state.csrf = (await api('/api/config')).csrf_token;
    await refreshAccount();
    for (const workspace of state.account.workspaces) { const option = el('option', '', workspace.name); option.value = workspace.workspace_id; option.selected = workspace.workspace_id === state.account.workspace_id; $('videoWorkspace').append(option); }
    state.config = await api('/api/video/config'); configView();
    const projects = await loadProjects(); const requested = new URL(location.href).searchParams.get('project');
    if (initialRevision === state.draftRevision && !state.project && !state.selectingProject && !state.switchingWorkspace) {
      if (requested && projects.some(project => project.project_id === requested)) await selectProject(requested);
      else if (projects.length) await selectProject(projects[0].project_id);
    }
    document.dispatchEvent(new CustomEvent('video-ready'));
    setInterval(() => { if (!document.hidden && state.account) refresh().catch(error => notice(error.message, true)); }, 8000);
  })().catch(error => notice(error.message, true));
})();
