(() => {
  'use strict';
  const { snapshot, captureContext, isCurrent, refreshProject, addReferences, api, refresh, notice, busy, mediaURL, el, uuid, ensureProject } = window.IWVideo;
  const $ = id => document.getElementById(id);
  const p = { id: null, data: null, config: null, quote: null, imageRequest: uuid(), scriptRequest: uuid(), quoteBody: '', quoteSequence: 0, loading: false, imageRefs: [], runRequests: new Map(), revision: 0 };
  const pipelineContext = () => ({...captureContext(), id: p.id, pipelineRevision: p.revision});
  const samePipeline = context => isCurrent(context) && context.id === p.id && context.pipelineRevision === p.revision;
  const labels = { queued: '排队中', running: '生成中', archiving: '保存中', completed: '已完成', failed: '生成失败', uncertain: '状态待确认' };
  const stages = ['image', 'script', 'video'];
  const panels = ['pipelineImages', 'pipelineScripts', 'pipelineVideos'];
  const stageLinks = [...document.querySelectorAll('[data-pipeline-step]')];
  const stageFromURL = () => stages[panels.indexOf(location.hash.slice(1))] || 'image';
  let initialProjectView = true;
  let currentStage = 'image';
  function showStage(stage, updateURL = true) {
    const index = stages.indexOf(stage); if (index < 0) return;
    currentStage = stage;
    for (const [i, id] of panels.entries()) $(id).hidden = i !== index;
    $('videoResults').hidden = stage !== 'video';
    for (const link of stageLinks) {
      if (link.dataset.pipelineStep === stage) link.setAttribute('aria-current', 'step'); else link.removeAttribute('aria-current');
    }
    if (updateURL && location.hash !== `#${panels[index]}`) {
      const url = new URL(location.href); url.hash = panels[index];
      history.replaceState(null, '', url);
    }
    $('pipelinePrevious').disabled = index === 0;
    $('pipelineNext').hidden = index === 2;
    $('pipelineNext').textContent = index === 0 ? '下一步：脚本' : '下一步：视频';
    $('pipelineStepLabel').textContent = `第 ${index + 1} 步，共 3 步`;
  }
  for (const [index, link] of stageLinks.entries()) {
    link.addEventListener('click', event => { event.preventDefault(); showStage(link.dataset.pipelineStep); });
    link.addEventListener('keydown', event => {
      const next = { ArrowRight: (index + 1) % stages.length, ArrowLeft: (index + stages.length - 1) % stages.length, Home: 0, End: stages.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault(); showStage(stages[next]); stageLinks[next].focus();
    });
  }
  function adjacentStage(offset) {
    showStage(stages[stages.indexOf(currentStage) + offset]);
    stageLinks[stages.indexOf(currentStage)].focus();
  }
  $('pipelinePrevious').addEventListener('click', () => adjacentStage(-1));
  $('pipelineNext').addEventListener('click', () => adjacentStage(1));
  window.addEventListener('hashchange', () => showStage(stageFromURL(), false));
  showStage(stageFromURL(), false);

  // Read the same task snapshots already used by the cards; navigation never manages jobs.
  function taskIndicator(stage, tasks) {
    const groups = [
      ['生成中', new Set(['running', 'generating'])],
      ['保存中', new Set(['archiving', 'checking'])],
      ['排队中', new Set(['queued'])],
      ['待确认', new Set(['uncertain'])]
    ];
    const summary = groups.map(([label, statuses]) => {
      const count = tasks.filter(task => statuses.has(task.status || task.current_job?.status)).length;
      return count ? `${label} ${count}` : '';
    }).filter(Boolean).join(' · ');
    const link = stageLinks[stages.indexOf(stage)], badge = link.querySelector('.pipeline-nav-status');
    badge.hidden = !summary; badge.textContent = summary;
    const name = link.querySelector('.pipeline-nav-title>span').textContent;
    link.title = `切换到${name}${summary ? `（${summary}）` : ''}`;
  }
  async function ensurePipeline() {
    const context = captureContext();
    const project = await ensureProject();
    if (!isCurrent(context, project.project_id)) throw new Error('项目已切换，请在当前项目重新操作');
    if (!p.data) await reload(true);
    if (!isCurrent(context, project.project_id) || !p.data || p.id !== project.project_id) throw new Error('项目尚未就绪，请重试');
    return project.project_id;
  }
  const action = (title, fn, secondary = true) => {
    const button = el('button', secondary ? 'secondary-button' : 'primary-button', title);
    button.type = 'button'; button.addEventListener('click', () => busy(button, fn)); return button;
  };
  function option(select, value, text, disabled = false) {
    const item = el('option', '', text); item.value = value; item.disabled = disabled; select.append(item);
  }
  function localScriptEstimate() {
    return window.IWScriptPricing.estimate(p.config, $('pipelineScriptModel').value, $('pipelineScriptThinking').value, snapshot().account?.credits, $('pipelineScriptType').value);
  }
  function scriptReady() { return Boolean(p.config && $('pipelineScriptRequest').value.trim() && $('pipelineScriptTemplate').value); }
  function updateScriptPrice() {
    let input; try { input = body(); } catch (_) { input = null; }
    const current = input && p.quoteBody === JSON.stringify(input) ? p.quote : null;
    const estimate = localScriptEstimate(), needsQuote = !estimate || Boolean(input?.storyboard_options);
    $('pipelineScriptPrice').textContent = current ? `${current.display_name} / ${current.thinking_level} · 本次 1 条：${window.IWScriptPricing.priceText(current.price)} · 当前余额：${current.remaining_credit === null ? '无限' : current.remaining_credit + ' 积分'}${current.storyboard_quote ? ' · 分镜：' + window.IWScriptPricing.priceText(current.storyboard_quote.price) : ''}${current.shortfall ? '。积分不足，本次需要 ' + (current.total_estimated_credit ?? current.price) + ' 积分，当前剩余 ' + current.remaining_credit + ' 积分。' : ''}` : window.IWScriptPricing.summary(estimate);
    const allowed = estimate ? estimate.can_generate && (!current || current.can_generate) : current?.can_generate;
    $('pipelineScriptGenerate').disabled = Boolean(p.scriptBusy || !scriptReady() || !allowed || (needsQuote && !current?.can_generate) || (current?.warnings?.length && !$('pipelineScriptWarningsAccepted').checked));
    $('pipelineScriptGenerate').textContent = p.scriptBusy ? '正在提交…' : '开始生成';
  }
  function invalidateQuote() {
    // A textarea blur emits change after input; unchanged data must not disable the clicked button.
    try { if (p.quote && p.quoteBody === JSON.stringify(body())) { updateScriptPrice(); return; } } catch (_) {}
    notice(''); p.quote = null; p.quoteBody = ''; p.quoteSequence = (p.quoteSequence || 0) + 1; clearTimeout(p.quoteTimer);
    $('pipelineScriptWarning').hidden = true; $('pipelineScriptWarningsAccepted').checked = false;
    updateScriptPrice();
    if (scriptReady() && p.data) p.quoteTimer = setTimeout(() => requestScriptQuote().catch(error => notice(error.message,true)),450);
  }
  async function requestScriptQuote() {
    if (!scriptReady() || !p.data) return null;
    const input = body(), context = pipelineContext(), sequence = ++p.quoteSequence;
    if (!samePipeline(context)) return null;
    try {
      const result = await api('/api/scripts/quote', { method: 'POST', body: JSON.stringify(input) });
      if (!samePipeline(context) || sequence !== p.quoteSequence || JSON.stringify(body()) !== JSON.stringify(input)) return null;
      p.quote = result; p.quoteBody = JSON.stringify(input); window.IWStoryboard?.showQuote('pipeline', result.storyboard_quote);
      $('pipelineScriptWarning').hidden = !result.warnings.length;
      $('pipelineScriptWarning').querySelector('span').textContent = result.warnings.join(' ') + ' 我已了解，仍按当前模型生成。';
      updateScriptPrice(); return result;
    } catch(error) {
      if (!samePipeline(context) || sequence !== p.quoteSequence) return null;
      updateScriptPrice();
      if (!localScriptEstimate() || input.storyboard_options) $('pipelineScriptPrice').textContent = error.message;
      return null;
    }
  }
  function scriptPendingId(input) {
    const key = 'video-script.pending.' + snapshot().account.account_id + '.' + snapshot().account.workspace_id + '.' + p.id;
    const fingerprint = JSON.stringify(input); let pending;
    try { pending = JSON.parse(localStorage.getItem(key)); } catch (_) {}
    if (!pending || pending.fingerprint !== fingerprint) pending = {fingerprint,request_id:uuid()};
    try { localStorage.setItem(key,JSON.stringify(pending)); } catch (_) {}
    return {key,request_id:pending.request_id};
  }
  function templates() {
    const previous = $('pipelineScriptTemplate').value;
    $('pipelineScriptTemplate').replaceChildren();
    for (const item of p.config?.templates || []) if (item.type === $('pipelineScriptType').value) option($('pipelineScriptTemplate'), item.template_id, item.name || item.template_name || item.template_id);
    if ([...$('pipelineScriptTemplate').options].some(o => o.value === previous)) $('pipelineScriptTemplate').value = previous;
  }
  function efforts() {
    const select = $('pipelineScriptThinking'), previous = select.value;
    select.replaceChildren();
    const model = p.config?.models.find(m => m.model_id === $('pipelineScriptModel').value);
    select.disabled = !model;
    if (!model) { option(select, '', '根据需求推荐'); return; }
    for (const level of model.thinking_levels) option(select, level.id, `${level.display_name} · ${window.IWScriptPricing.priceText(level.price)}`, model.available === false || !Number.isInteger(level.price));
    select.value = [...select.options].some(o => o.value === previous) ? previous : [...select.options].some(o => o.value === 'high') ? 'high' : select.options[0]?.value || '';
  }
  function models() {
    const select = $('pipelineScriptModel'), previous = select.value;
    select.replaceChildren(); option(select, 'recommended', '系统推荐');
    for (const model of p.config?.models || []) option(select, model.model_id, model.display_name + (model.available ? '' : ' · 当前模型不可用'), model.available === false);
    select.value = [...select.options].some(o => o.value === previous) ? previous : 'recommended';
    efforts(); templates(); updateScriptPrice();
  }
  function body() {
    if (!p.data) throw new Error('项目正在初始化，请稍候');
    const choice = $('pipelineScriptModel').value;
    const result = { subproject_id: p.data.subproject_id, generation_type: $('pipelineScriptType').value,
      template_id: $('pipelineScriptTemplate').value, input_request: $('pipelineScriptRequest').value.trim(), selection_mode: choice === 'recommended' ? 'recommended' : 'manual' };
    if (choice !== 'recommended') { result.model_id = choice; result.thinking_level = $('pipelineScriptThinking').value; }
    const storyboard = window.IWStoryboard?.options('pipeline'); if (storyboard) result.storyboard_options = { ...storyboard, video_project_id: p.id };
    return result;
  }
  window.IWStoryboard?.mountScript({ api, getContext: () => ({ video_project_id: p.id, subproject_id: p.data?.subproject_id, workspace_id: snapshot().account?.workspace_id, account_id: snapshot().account?.account_id }), invalidate: invalidateQuote, anchor: $('pipelineScriptPrice'), prefix: 'pipeline' });
  function refs(project) {
    const signature = JSON.stringify(project.assets);
    if (p.refSignature === signature) return;
    p.refSignature = signature;
    p.imageRefs = p.imageRefs.filter(id => project.assets.some(a => a.asset_id === id));
    const container = $('pipelineImageReferences'); container.replaceChildren();
    for (const asset of project.assets) {
      const label = el('label', 'video-reference-choice'), input = el('input'); input.type = 'checkbox'; input.value = asset.asset_id; input.checked = p.imageRefs.includes(asset.asset_id);
      input.addEventListener('change', () => { p.imageRefs = p.imageRefs.filter(id => id !== asset.asset_id); if (input.checked) p.imageRefs.push(asset.asset_id); p.imageRequest = uuid(); });
      const image = el('img'); image.src = mediaURL(asset.url); image.alt = '';
      label.append(input, image, el('span', '', asset.filename)); container.append(label);
    }
    if (!project.assets.length) container.append(el('p', 'field-hint', '可直接文生图，也可以先上传参考文件。'));
  }
  function renderImages(data, id) {
    const context = pipelineContext();
    const list = $('pipelineImageTasks'); list.replaceChildren();
    for (const task of data.image_tasks) {
      const card = el('article', 'pipeline-task');
      card.append(el('h3', '', task.name || task.prompt.slice(0, 100)), el('p', '', `${task.status_label} · ${task.input_count} 张参考图 · ${task.estimated_credit} 积分`));
      const actions = el('div', 'pipeline-task-actions');
      if (task.can_generate || task.can_retry) actions.append(action(`${task.can_retry ? '重试生成' : '生成图片'} · ${task.estimated_credit} 积分`, async () => {
        if (!samePipeline(context)) return;
        const key = task.task_id + ':' + (task.current_job?.job_id || 'first');
        if (!p.runRequests.has(key)) p.runRequests.set(key, uuid());
        const request = { request_id: p.runRequests.get(key), confirm_generation: true };
        if (task.can_retry) request.retry_of = task.current_job?.job_id || task.last_execution_id;
        await api(`/api/tasks/${task.task_id}/run`, { method: 'POST', body: JSON.stringify(request) });
        if (!samePipeline(context)) return;
        notice('图片已进入生成队列，成功后可加入项目参考图库。');
        await reload(); await refresh(context);
      }, false));
      if (task.error_summary) card.append(el('p', 'field-hint', task.error_summary));
      card.append(actions);
      for (const artifact of task.outputs || []) {
        const block = el('div', 'pipeline-image-result'), image = el('img'); image.src = mediaURL(artifact.url); image.alt = task.name || '生成图片';
        const download = el('a', 'text-button', '下载图片'); download.href = mediaURL(artifact.download_url);
        image.addEventListener('click', () => window.IWAssetActions.preview({...artifact,media_type:'image'}));
        block.append(image, action('加入参考图', async () => {
          if (!samePipeline(context)) return;
          await api(`/api/video/projects/${id}/images/from-artifact`, { method: 'POST', body: JSON.stringify({ artifact_id: artifact.artifact_id }) });
          if (samePipeline(context)) { notice('图片已加入项目参考图库，可用于 H3 视频生成。'); await refresh(context); }
        }), download, imageActions(artifact)); card.append(block);
      }
      list.append(card);
    }
  }
  function renderScripts(data, id) {
    const context = pipelineContext();
    const list = $('pipelineScriptTasks'); list.replaceChildren();
    for (const task of data.script_tasks) {
      const card = el('article', 'pipeline-task'), selected = data.active_script_task_id === task.task_id;
      const name = task.source === 'upload' ? '上传脚本' : task.generation_type === 'screenplay' ? '剧情剧本' : '视频脚本';
      card.append(el('h3', '', `${name}${task.output_version ? ' · v' + task.output_version : ''}${selected ? ' · 当前使用' : ''}`), el('p', '', `${labels[task.status] || task.status} · ${task.model || '手动保存'}${task.thinking_level ? ' / ' + task.thinking_level : ''} · ${task.price} 积分`));
      const storyboardStage=el('div','storyboard-linked-stage'); card.append(storyboardStage); window.IWStoryboard?.linkedStage(task,storyboardStage,'pipeline');
      if (task.error) card.append(el('p', '', task.error));
      const actions = el('div', 'pipeline-task-actions');
      if (task.status === 'completed') {
        actions.append(action('为此版本规划分镜', async () => { if (!samePipeline(context)) return; const detail = await api(`/api/scripts/tasks/${task.task_id}`); if (!samePipeline(context)) return; window.IWStoryboard?.planScript(detail, 'pipeline'); }));
        actions.append(action(selected ? '查看当前脚本' : '预览脚本', async () => {
          const result = await api(`/api/scripts/tasks/${task.task_id}`);
          if (!samePipeline(context)) return;
          const old = card.querySelector('pre'); if (old) old.remove(); else card.append(el('pre', 'pipeline-script-text', result.text));
        }));
        if (!selected) actions.append(action('用于当前项目', async () => {
          if (!samePipeline(context)) return;
          await api(`/api/video/projects/${id}/script/from-task`, { method: 'POST', body: JSON.stringify({ task_id: task.task_id }) });
          if (samePipeline(context)) { notice('已保存为当前项目脚本，可以继续第三步视频创作。'); await refresh(context); }
        }, false));
        const download = el('a', 'text-button', '下载 TXT'); download.href = mediaURL(`/api/scripts/tasks/${task.task_id}/download`); actions.append(download);
        const editor = `/scripts?${new URLSearchParams({workspace_id:snapshot().account.workspace_id,main_project_id:data.main_project_id,subproject_id:data.subproject_id,task_id:task.task_id,action:'edit'})}`;
        actions.append(window.IWAssetActions.menu([{label:'继续修改',href:editor},
          ...(window.IWStoryboard ? [{label:'生成分镜',run:()=>window.IWStoryboard.planScript(task,'pipeline')}] : []),
          ...(p.config?.capabilities?.creative_resources ? [{label:'保存为模板',href:editor.replace('action=edit','action=template')}] : []),
          {label:'下载 TXT',href:download.href}],notice));
      }
      card.append(actions); list.append(card);
    }
  }
  async function reload(initial = false) {
    const context = pipelineContext(), id = p.id; if (!id || !samePipeline(context) || (p.loading && !initial)) return;
    p.loading = true;
    try {
      const data = await api(`/api/video/projects/${id}/pipeline`, initial ? { method: 'POST', body: '{}' } : {});
      if (!samePipeline(context)) return;
      p.data = data;
      updateScriptPrice();
      await reloadTray();
      if (!samePipeline(context)) return;
      await requestedAsset();
      if (!samePipeline(context)) return;
      taskIndicator('image', data.image_tasks);
      taskIndicator('script', data.script_tasks);
      const imageSignature = JSON.stringify(data.image_tasks);
      const scriptSignature = JSON.stringify([data.script_tasks, data.active_script_task_id]);
      if (p.imageSignature !== imageSignature) { renderImages(data, id); p.imageSignature = imageSignature; }
      if (p.scriptSignature !== scriptSignature) { renderScripts(data, id); p.scriptSignature = scriptSignature; }
      const editor = document.querySelector('#pipelineScripts a[href^="/scripts"]');
      editor.href = `/scripts?workspace_id=${encodeURIComponent(snapshot().account.workspace_id)}&main_project_id=${data.main_project_id}&subproject_id=${data.subproject_id}`;
    } finally { if (samePipeline(context)) p.loading = false; }
  }
  function resetProject(preserveDraft = false) {
    p.revision++; p.context = null; p.id = null; p.data = null; p.loading = false; p.imageRefs = []; p.imageRequest = uuid();
    p.scriptSubmission = null; p.scriptBusy = false;
    for (const control of $('pipelineScriptForm').querySelectorAll('input,textarea,select,button')) control.disabled = false;
    p.refSignature = ''; p.imageSignature = ''; p.scriptSignature = ''; p.traySignature = ''; p.tray = []; $('assetTrayGrid').replaceChildren(); $('assetTrayCount').textContent = '0';
    if (!preserveDraft) { $('pipelineImageForm').reset(); $('pipelineScriptForm').reset(); showStage(initialProjectView ? stageFromURL() : 'image', !initialProjectView); document.querySelector('[data-pipeline-tab="image"][data-mode="upload"]').click(); }
    invalidateQuote(); $('pipelineImageReferences').replaceChildren();
    $('pipelineImageTasks').replaceChildren(); $('pipelineScriptTasks').replaceChildren();
    for (const stage of stages) taskIndicator(stage, []);
  }
  document.addEventListener('video-project-cleared', () => resetProject());
  function loadScriptConfig() { const context = captureContext(); api('/api/scripts/config').then(config => { if (!isCurrent(context)) return; p.config = config; models(); }).catch(error => { if (isCurrent(context)) notice(error.message, true); }); }
  document.addEventListener('video-ready', () => { initialProjectView = false; loadScriptConfig(); });
  document.addEventListener('video-project-view', event => {
    const project = event.detail.project;
    const changed = p.id !== project.project_id || !p.context || !isCurrent(p.context);
    if (changed) { resetProject(event.detail.preserveDraft); p.id = project.project_id; }
    p.context = captureContext();
    taskIndicator('video', project.shots);
    refs(project);
    reload(changed || !p.data).catch(error => notice(error.message, true));
    loadScriptConfig();
  });
  for (const button of document.querySelectorAll('[data-pipeline-tab]')) button.addEventListener('click', () => {
    const stage = button.dataset.pipelineTab, mode = button.dataset.mode;
    for (const other of document.querySelectorAll(`[data-pipeline-tab="${stage}"]`)) { const selected = other === button; other.classList.toggle('is-active', selected); other.setAttribute('aria-pressed', String(selected)); }
    for (const pane of document.querySelectorAll(`[data-pipeline-pane^="${stage}:"]`)) pane.hidden = pane.dataset.pipelinePane !== `${stage}:${mode}`;
  });
  $('pipelineImageForm').addEventListener('input', () => { p.imageRequest = uuid(); });
  $('pipelineImageForm').addEventListener('submit', event => {
    event.preventDefault(); busy(event.submitter, async () => {
      if (p.imageRefs.length > 8) throw new Error('图片生成最多选择 8 张参考图');
      const id = await ensurePipeline();
      const context = pipelineContext();
      await api(`/api/video/projects/${id}/images`, { method: 'POST', body: JSON.stringify({ request_id: p.imageRequest, prompt: $('pipelineImagePrompt').value.trim(), aspect_ratio: $('pipelineImageRatio').value, reference_ids: p.imageRefs }) });
      if (samePipeline(context)) { notice('生图任务已保存。核对下方积分后点击“生成图片”。'); await reload(); }
    });
  });
  function assetContext() {
    const storyboard = currentStage === 'video' ? window.IWStoryboard?.assetTarget() : null;
    return { ...captureContext(), storyboard, imageRequest: p.imageRequest,
      purpose: storyboard ? 'storyboard' : currentStage === 'image' ? (document.querySelector('[data-pipeline-pane="image:generate"]').hidden ? 'library' : 'reference') : currentStage === 'video' && !document.querySelector('[data-pipeline-pane="video:generate"]').hidden ? 'video' : null };
  }
  function sameAssetContext(context, id) { return isCurrent(context, id); }
  async function attachAssets(assets, purpose, context, id) {
    if (!sameAssetContext(context, id)) return notice('图片已保存到原项目；当前页面的素材选择未改变。');
    if (purpose === 'storyboard' || (context.storyboard && ['video','first','last'].includes(purpose))) { window.IWStoryboard.attachAssets(assets,context.storyboard,purpose); return; }
    const ids = assets.map(asset => asset.asset_id || asset.artifact_id);
    if (purpose === 'reference') {
      if (context.imageRequest !== p.imageRequest) return notice('图片已保存；当前生图草稿已改变，请重新选择参考图。');
      const next = [...new Set([...p.imageRefs, ...ids])];
      if (next.length > 8) throw new Error('最多选择 8 张生图参考；图片已入库，请减少选择后重试');
      p.imageRefs = next; p.imageRequest = uuid(); p.refSignature = ''; refs(snapshot().project);
    } else if (['video', 'first', 'last'].includes(purpose)) {
      if (!addReferences(ids, purpose, context)) return notice('图片已保存；当前视频草稿已改变，请重新选择参考图。');
    }
    notice(purpose === 'library' ? '图片已保存到项目素材库。' : '图片已添加到当前任务，已有素材已保留。');
  }
  window.IWImageInput.configure({ notice, context: assetContext,
    limits: () => ({count: 9, single: snapshot().config?.max_asset_bytes || 20*1024*1024, total: 40*1024*1024}),
    async receive(files, purpose, context) {
      if (!isCurrent(context)) throw new Error('项目已切换，请在当前项目重新添加');
      const id = await ensurePipeline();
      if (!sameAssetContext(context, id)) throw new Error('项目已切换，请重新添加');
      if (context.id == null) { context.id = id; context.imageRequest = p.imageRequest; }
      const form = new FormData(); files.forEach(file => form.append('files', file));
      const result = await api(`/api/video/projects/${id}/assets`, {method:'POST',body:form,headers:{'X-Workspace-ID':context.workspace}});
      if (sameAssetContext(context,id)) {
        const project = await refreshProject(context, {notify: false});
        if (project && sameAssetContext(context,id)) { refs(project); await attachAssets(result.assets,purpose,context,id); }
      } else notice('图片已保存到原项目。当前页面的素材选择未改变。');
    }
  });
  window.IWImageInput.bind({input:$('videoAssets'),zone:document.querySelector('[data-pipeline-pane="image:upload"]'),purpose:'library'});
  window.IWImageInput.bind({input:$('pipelineImageReferenceFiles'),zone:$('pipelineImageForm'),purpose:'reference'});
  window.IWImageInput.bind({zone:$('videoShotForm'),purpose:'video'});
  $('pipelineChooseImages').addEventListener('click', async () => {
    try {
      const id = await ensurePipeline(), context = assetContext();
      const result = await api(`/api/subprojects/${p.data.subproject_id}/assets`);
      const assets = await window.IWImageInput.selectAssets(result.assets);
      if (!sameAssetContext(context,id) || !assets.length) return;
      for (const artifact of assets) {
        if (!sameAssetContext(context,id)) return;
        await api(`/api/video/projects/${id}/images/from-artifact`,{method:'POST',body:JSON.stringify({artifact_id:artifact.artifact_id}),headers:{'X-Workspace-ID':context.workspace}});
      }
      const project = await refreshProject(context, {notify: false});
      if (!project || !sameAssetContext(context,id)) return;
      p.refSignature=''; refs(project);
      await attachAssets(assets,context.purpose || 'library',context,id);
    } catch(error) { notice(error.message,true); }
  });
  const assetKey = asset => asset.artifact_id || asset.asset_id;
  function imageView(asset) { return {...asset, artifact_id:assetKey(asset), media_type:'image', filename:asset.filename || '项目图片', url:mediaURL(asset.url), download_url:mediaURL(asset.download_url || asset.url)}; }
  async function useImage(asset, purpose) {
    const id = await ensurePipeline(), context = assetContext();
    if (asset.subproject_id && asset.subproject_id !== p.data.subproject_id) throw new Error('素材不属于当前子项目');
    if (!asset.asset_id || asset.artifact_id) await api(`/api/video/projects/${id}/images/from-artifact`,{method:'POST',body:JSON.stringify({artifact_id:assetKey(asset)})});
    const project = await refreshProject(context, {notify: false});
    if (!project || !sameAssetContext(context,id)) return;
    p.refSignature=''; refs(project);
    await attachAssets([asset],purpose,context,id);
    if (purpose === 'reference') { showStage('image'); document.querySelector('[data-pipeline-tab="image"][data-mode="generate"]').click(); }
    else if (purpose !== 'library') { showStage('video'); document.querySelector('[data-pipeline-tab="video"][data-mode="generate"]').click(); }
  }
  function imageEditorURL(asset, purpose='edit') {
    const url = new URL('/', location.origin);
    url.search = new URLSearchParams({workspace_id:snapshot().account.workspace_id,main_project_id:p.data.main_project_id,subproject_id:p.data.subproject_id,asset_id:assetKey(asset),asset_action:purpose});
    return url.pathname + url.search;
  }
  async function canonicalVideo(asset) {
    if (asset.media_type !== 'video' || asset.artifact_id) return asset;
    await reloadTray();
    const found=p.tray.find(item=>item.media_type==='video' && item.shot_id===asset.shot_id);
    if (!found) throw new Error('视频素材尚未就绪，请刷新后重试');
    return {...asset,artifact_id:found.artifact_id};
  }
  async function trayChange(asset, actionName) {
    const context = pipelineContext();
    asset=await canonicalVideo(asset);
    const id=context.id; if (!id || !samePipeline(context)) return;
    await api(`/api/video/projects/${id}/tray`,{method:'POST',body:JSON.stringify({artifact_id:assetKey(asset),media_type:asset.media_type || 'image',action:actionName,...(asset.shot_id ? {shot_id:asset.shot_id}: {})})});
    if(samePipeline(context)) { await reloadTray(); notice(actionName==='remove' ? '已移出暂存区，原始素材仍在项目中。' : '素材已加入暂存区。'); }
  }
  function imageActions(asset, inTray=false) {
    const item=imageView(asset);
    const actions=[{label:'预览',run:()=>window.IWAssetActions.preview(item)}];
    if(asset.artifact_id) actions.push({label:'继续修改',run:async()=>{await ensurePipeline(); location.assign(imageEditorURL(asset));}}, {label:'设为参考图',run:()=>useImage(asset,'reference')});
    actions.push({label:'用于视频生成',run:()=>useImage(asset,'video')},{label:'设为首帧',run:()=>useImage(asset,'first')},{label:'设为尾帧',run:()=>useImage(asset,'last')});
    if(asset.artifact_id) actions.push({label:inTray?'移出暂存区':'加入素材暂存区',run:()=>trayChange(item,inTray?'remove':'add')});
    actions.push({label:'下载',href:item.download_url});
    return window.IWAssetActions.menu(actions,notice);
  }
  async function bindVideo(asset) {
    const context=assetContext(),id=p.id;
    asset=await canonicalVideo(asset);
    if (!sameAssetContext(context,id)) return;
    const targets=snapshot().project.shots.filter(shot=>shot.shot_id!==asset.shot_id && shot.status==='draft' && !shot.bound_clip);
    if(!targets.length) throw new Error('请先保存一个尚未生成、未绑定视频的镜头，再绑定片段');
    const sid=await window.IWImageInput.choose('选择要绑定视频的镜头',targets.map(shot=>({value:shot.shot_id,label:shot.name})));
    if(!sid || !sameAssetContext(context,id)) return;
    await api(`/api/video/projects/${id}/shots/${sid}/clip`,{method:'POST',body:JSON.stringify({source_shot_id:asset.shot_id,artifact_id:asset.artifact_id})});
    if (sameAssetContext(context,id)) { await refresh(context); notice('视频片段已绑定到镜头，未复制原文件。'); }
  }
  function videoActions(asset,inTray=false) {
    return window.IWAssetActions.menu([
      {label:'预览',run:()=>window.IWAssetActions.preview(asset)},
      ...[false,true].map(last=>({label:last?'提取尾帧':'提取首帧',run:async()=>{
        const context=assetContext(),id=p.id;
        const file=await window.IWAssetActions.extractFrame(asset,last);
        if(!sameAssetContext(context,id)) throw new Error('项目已切换，请重新提取画面');
        await window.IWImageInput.receive([file],'library');
      }})),
      {label:'绑定镜头',run:()=>bindVideo(asset)},
      {label:inTray?'移出暂存区':'加入素材暂存区',run:()=>trayChange(asset,inTray?'remove':'add')},
      {label:'下载',href:asset.download_url}
    ],notice);
  }
  async function reloadTray() {
    const context=pipelineContext(), id=p.id; if(!id || !p.data || !samePipeline(context)) return;
    const result=await api(`/api/video/projects/${id}/tray`);
    if(!samePipeline(context)) return;
    p.tray=result.assets;
    const signature=JSON.stringify(result); if(signature===p.traySignature) return;
    p.traySignature=signature; $('assetTrayCount').textContent=String(result.assets.length); $('assetTrayGrid').replaceChildren();
    for(const asset of result.assets) {
      const card=el('article','asset-tray-card'); card.dataset.artifactId=asset.artifact_id;
      const preview=el('button'); preview.type='button'; preview.setAttribute('aria-label',`预览 ${asset.filename}`);
      if(asset.media_type==='image') { const img=el('img'); img.src=mediaURL(asset.url); img.alt=asset.filename; img.loading='lazy'; preview.append(img); }
      else preview.textContent='▶ 视频片段';
      preview.addEventListener('click',()=>window.IWAssetActions.preview(asset));
      card.append(preview,el('p','',asset.filename),asset.media_type==='image'?imageActions(asset,true):videoActions(asset,true));
      window.IWAssetActions.draggable(card,asset,{project_id:id,subproject_id:p.data.subproject_id,workspace_id:snapshot().account.workspace_id});
      $('assetTrayGrid').append(card);
    }
    if(!result.assets.length) $('assetTrayGrid').append(el('p','field-hint','上传、生成或主动加入的素材会出现在这里。'));
  }
  async function droppedAsset(asset,purpose) {
    if(asset.workspace_id!==snapshot().account.workspace_id || asset.project_id!==p.id || asset.subproject_id!==p.data?.subproject_id) throw new Error('只能引用当前项目和工作空间的素材');
    const item=p.tray.find(item=>item.artifact_id===asset.artifact_id);
    if(!item || item.media_type!=='image') throw new Error('此输入区只接受当前项目图片');
    await useImage(item,purpose);
  }
  window.IWAssetActions.dropzone($('pipelineImageReferences'),asset=>droppedAsset(asset,'reference'),notice);
  window.IWAssetActions.dropzone($('videoReferenceChoices'),asset=>droppedAsset(asset,'video'),notice);
  document.addEventListener('asset-library-change',()=>reloadTray().catch(error=>notice(error.message,true)));
  let handoffDone=false;
  async function requestedAsset() {
    if(handoffDone || !p.data) return;
    const params=new URL(location.href).searchParams, aid=params.get('asset_id'); if(!aid) return;
    handoffDone=true;
    const asset=p.tray.find(item=>item.artifact_id===aid && item.media_type==='image');
    if(!asset) return notice('传入的图片不属于当前项目，请从暂存区重新选择。',true);
    const purpose=params.get('asset_action'); await useImage(asset,['video','first','last','reference'].includes(purpose)?purpose:'video');
  }
  window.IWAssetTray={imageActions,videoActions};
  $('pipelineScriptType').addEventListener('change', templates);
  $('pipelineScriptModel').addEventListener('change', efforts);
  $('pipelineScriptForm').addEventListener('input', event => { if (event.target !== $('pipelineScriptWarningsAccepted')) invalidateQuote(); });
  $('pipelineScriptForm').addEventListener('change', event => { if (event.target !== $('pipelineScriptWarningsAccepted')) invalidateQuote(); });
  $('pipelineScriptWarningsAccepted').addEventListener('change', updateScriptPrice);
  $('pipelineScriptForm').addEventListener('submit', async event => {
    event.preventDefault(); if (p.scriptBusy || !$('pipelineScriptForm').reportValidity()) return;
    const submission = {}; let context; p.scriptSubmission = submission;
    p.scriptBusy = true; clearTimeout(p.quoteTimer);
    const controls = [...$('pipelineScriptForm').querySelectorAll('input,textarea,select,button')]; controls.forEach(control => control.disabled = true); updateScriptPrice();
    try {
      await ensurePipeline();
      // Creating the first project resets the view while retaining this draft.
      p.scriptSubmission = submission; p.scriptBusy = true;
      controls.forEach(control => control.disabled = true);
      context = pipelineContext(); const input = body();
      let current = p.quoteBody === JSON.stringify(input) ? p.quote : null;
      if (input.selection_mode !== 'manual' || input.storyboard_options) {
        if (!current) current = await requestScriptQuote();
        if (!current?.can_generate) return;
      } else if (!localScriptEstimate()?.can_generate) return;
      if (current && !current.can_generate) return;
      if (current?.warnings?.length && !$('pipelineScriptWarningsAccepted').checked) { $('pipelineScriptWarning').hidden = false; return; }
      if (!samePipeline(context)) return;
      const pending = scriptPendingId(input);
      await api('/api/scripts/tasks', { method: 'POST', body: JSON.stringify({ ...input, request_id: pending.request_id,
        ...(current?.quote_signature ? {quote_signature:current.quote_signature} : {}), warnings_confirmed: $('pipelineScriptWarningsAccepted').checked,
        storyboard_confirmed: Boolean(input.storyboard_options) }) });
      try { localStorage.removeItem(pending.key); } catch (_) {}
      if (samePipeline(context)) { invalidateQuote(); notice('脚本已提交。完成后可预览，并点击“用于当前项目”。'); await reload(); await refresh(context); }
    } catch(error) { if ((!context || samePipeline(context)) && p.scriptSubmission === submission) { notice(error.message,true); await requestScriptQuote(); } }
    finally { if (p.scriptSubmission === submission) { p.scriptSubmission = null; p.scriptBusy = false; controls.forEach(control => control.disabled = false); $('pipelineScriptThinking').disabled = $('pipelineScriptModel').value === 'recommended'; updateScriptPrice(); } }
  });
  async function uploadFile(input, stage) {
    const file = input.files[0]; if (!file) return;
    input.disabled = true;
    try {
      const limit = stage === 'script' ? 256 * 1024 : 40 * 1024 * 1024;
      if (!file.size || file.size > limit) throw new Error(stage === 'script' ? 'TXT 最大 256 KB' : 'MP4 最大 40 MB');
      const id = await ensurePipeline();
      const context = pipelineContext();
      const form = new FormData(); form.append('file', file); form.append('request_id', uuid());
      await api(`/api/video/projects/${id}/${stage === 'script' ? 'script' : 'videos'}/upload`, { method: 'POST', body: form });
      if (samePipeline(context)) { notice(stage === 'script' ? 'TXT 已保存为当前项目脚本，未扣积分。' : 'MP4 已保存，可在项目成片中预览和下载，未扣积分。'); await refresh(context); }
    } catch (error) { notice(error.message, true); } finally { input.disabled = false; input.value = ''; }
  }
  $('pipelineScriptUpload').addEventListener('change', event => uploadFile(event.target, 'script'));
  $('pipelineVideoUpload').addEventListener('change', event => uploadFile(event.target, 'video'));
  $('pipelineUseScript').addEventListener('click', () => {
    const text = snapshot().project?.script;
    if (!text) return notice('请先在第二步生成或上传脚本，并选为当前项目脚本。', true);
    if (text.length > 12000) return notice('整份脚本超过单镜头 12000 字限制，请从第二步选择需要的段落填入。', true);
    if ($('videoShotPrompt').value.trim()) return notice('镜头描述已有内容。请先保留或清空，再填入当前脚本。', true);
    $('videoShotPrompt').value = text; $('videoShotPrompt').dispatchEvent(new Event('input', {bubbles:true})); $('videoShotPrompt').focus(); notice('已填入当前脚本，请按这一镜需要调整画面、动作与声音。');
  });
})();
