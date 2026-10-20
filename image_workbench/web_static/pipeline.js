(() => {
  'use strict';
  const { state, api, refresh, notice, busy, mediaURL, el, uuid, ensureProject } = window.IWVideo;
  const $ = id => document.getElementById(id);
  const p = { id: null, data: null, config: null, quote: null, imageRequest: uuid(), scriptRequest: uuid(), quoteBody: '', loading: false, imageRefs: [], runRequests: new Map(), revision: 0 };
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
    const project = await ensureProject();
    if (!p.data) await reload(true);
    if (!p.data || p.id !== project.project_id) throw new Error('项目尚未就绪，请重试');
    return project.project_id;
  }
  const action = (title, fn, secondary = true) => {
    const button = el('button', secondary ? 'secondary-button' : 'primary-button', title);
    button.type = 'button'; button.addEventListener('click', () => busy(button, fn)); return button;
  };
  function option(select, value, text, disabled = false) {
    const item = el('option', '', text); item.value = value; item.disabled = disabled; select.append(item);
  }
  function invalidateQuote() {
    notice('');
    p.quote = null; p.quoteBody = ''; p.scriptRequest = uuid();
    $('pipelineScriptGenerate').disabled = true;
    $('pipelineScriptGenerate').textContent = '生成 TXT';
    $('pipelineScriptPrice').textContent = '填写需求后查看报价';
    $('pipelineScriptWarning').hidden = true; $('pipelineScriptWarningsAccepted').checked = false;
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
    for (const level of model.thinking_levels) option(select, level.id, `${level.display_name} · ${level.price == null ? '暂未定价' : level.price + ' 积分'}`);
    select.value = [...select.options].some(o => o.value === previous) ? previous : [...select.options].some(o => o.value === 'high') ? 'high' : select.options[0]?.value || '';
  }
  function models() {
    const select = $('pipelineScriptModel'), previous = select.value;
    select.replaceChildren(); option(select, 'recommended', '系统推荐');
    for (const model of p.config?.models || []) option(select, model.model_id, model.display_name + (model.available ? '' : ' · 暂不可用'));
    select.value = [...select.options].some(o => o.value === previous) ? previous : 'recommended';
    efforts(); templates();
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
  window.IWStoryboard?.mountScript({ api, getContext: () => ({ video_project_id: p.id, subproject_id: p.data?.subproject_id, workspace_id: state.account?.workspace_id, account_id: state.account?.account_id }), invalidate: invalidateQuote, anchor: $('pipelineScriptPrice'), prefix: 'pipeline' });
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
    const list = $('pipelineImageTasks'); list.replaceChildren();
    for (const task of data.image_tasks) {
      const card = el('article', 'pipeline-task');
      card.append(el('h3', '', task.name || task.prompt.slice(0, 100)), el('p', '', `${task.status_label} · ${task.input_count} 张参考图 · ${task.estimated_credit} 积分`));
      const actions = el('div', 'pipeline-task-actions');
      if (task.can_generate || task.can_retry) actions.append(action(`${task.can_retry ? '重试生成' : '生成图片'} · ${task.estimated_credit} 积分`, async () => {
        const key = task.task_id + ':' + (task.current_job?.job_id || 'first');
        if (!p.runRequests.has(key)) p.runRequests.set(key, uuid());
        const request = { request_id: p.runRequests.get(key), confirm_generation: true };
        if (task.can_retry) request.retry_of = task.current_job?.job_id || task.last_execution_id;
        await api(`/api/tasks/${task.task_id}/run`, { method: 'POST', body: JSON.stringify(request) });
        notice('图片已进入生成队列，成功后可加入项目参考图库。');
        if (p.id === id) await reload(); await refresh();
      }, false));
      if (task.error_summary) card.append(el('p', 'field-hint', task.error_summary));
      card.append(actions);
      for (const artifact of task.outputs || []) {
        const block = el('div', 'pipeline-image-result'), image = el('img'); image.src = mediaURL(artifact.url); image.alt = task.name || '生成图片';
        const download = el('a', 'text-button', '下载图片'); download.href = mediaURL(artifact.download_url);
        block.append(image, action('加入参考图', async () => {
          await api(`/api/video/projects/${id}/images/from-artifact`, { method: 'POST', body: JSON.stringify({ artifact_id: artifact.artifact_id }) });
          notice('图片已加入项目参考图库，可用于 H3 视频生成。'); if (p.id === id) await refresh();
        }), download); card.append(block);
      }
      list.append(card);
    }
  }
  function renderScripts(data, id) {
    const list = $('pipelineScriptTasks'); list.replaceChildren();
    for (const task of data.script_tasks) {
      const card = el('article', 'pipeline-task'), selected = data.active_script_task_id === task.task_id;
      const name = task.source === 'upload' ? '上传脚本' : task.generation_type === 'screenplay' ? '剧情剧本' : '视频脚本';
      card.append(el('h3', '', `${name}${task.output_version ? ' · v' + task.output_version : ''}${selected ? ' · 当前使用' : ''}`), el('p', '', `${labels[task.status] || task.status} · ${task.model || '手动保存'}${task.thinking_level ? ' / ' + task.thinking_level : ''} · ${task.price} 积分`));
      const storyboardStage=el('div','storyboard-linked-stage'); card.append(storyboardStage); window.IWStoryboard?.linkedStage(task,storyboardStage,'pipeline');
      if (task.error) card.append(el('p', '', task.error));
      const actions = el('div', 'pipeline-task-actions');
      if (task.status === 'completed') {
        actions.append(action('为此版本规划分镜', async () => { const detail = await api(`/api/scripts/tasks/${task.task_id}`); if (p.id !== id) return; window.IWStoryboard?.planScript(detail, 'pipeline'); }));
        actions.append(action(selected ? '查看当前脚本' : '预览脚本', async () => {
          const result = await api(`/api/scripts/tasks/${task.task_id}`);
          const old = card.querySelector('pre'); if (old) old.remove(); else card.append(el('pre', 'pipeline-script-text', result.text));
        }));
        if (!selected) actions.append(action('用于当前项目', async () => {
          await api(`/api/video/projects/${id}/script/from-task`, { method: 'POST', body: JSON.stringify({ task_id: task.task_id }) });
          notice('已保存为当前项目脚本，可以继续第三步视频创作。'); if (p.id === id) await refresh();
        }, false));
        const download = el('a', 'text-button', '下载 TXT'); download.href = mediaURL(`/api/scripts/tasks/${task.task_id}/download`); actions.append(download);
      }
      card.append(actions); list.append(card);
    }
  }
  async function reload(initial = false) {
    const id = p.id, revision = p.revision; if (!id || (p.loading && !initial)) return;
    p.loading = true;
    try {
      const data = await api(`/api/video/projects/${id}/pipeline`, initial ? { method: 'POST', body: '{}' } : {});
      if (p.id !== id || p.revision !== revision) return;
      p.data = data;
      taskIndicator('image', data.image_tasks);
      taskIndicator('script', data.script_tasks);
      const imageSignature = JSON.stringify(data.image_tasks);
      const scriptSignature = JSON.stringify([data.script_tasks, data.active_script_task_id]);
      if (p.imageSignature !== imageSignature) { renderImages(data, id); p.imageSignature = imageSignature; }
      if (p.scriptSignature !== scriptSignature) { renderScripts(data, id); p.scriptSignature = scriptSignature; }
      const editor = document.querySelector('#pipelineScripts a[href^="/scripts"]');
      editor.href = `/scripts?workspace_id=${encodeURIComponent(state.account.workspace_id)}&main_project_id=${data.main_project_id}&subproject_id=${data.subproject_id}`;
    } finally { if (p.id === id && p.revision === revision) p.loading = false; }
  }
  function resetProject(preserveDraft = false) {
    p.revision++; p.id = null; p.data = null; p.loading = false; p.imageRefs = []; p.imageRequest = uuid();
    p.refSignature = ''; p.imageSignature = ''; p.scriptSignature = '';
    if (!preserveDraft) { $('pipelineImageForm').reset(); $('pipelineScriptForm').reset(); showStage(initialProjectView ? stageFromURL() : 'image', !initialProjectView); document.querySelector('[data-pipeline-tab="image"][data-mode="upload"]').click(); }
    invalidateQuote(); $('pipelineImageReferences').replaceChildren();
    $('pipelineImageTasks').replaceChildren(); $('pipelineScriptTasks').replaceChildren();
    for (const stage of stages) taskIndicator(stage, []);
  }
  document.addEventListener('video-project-cleared', () => resetProject());
  function loadScriptConfig() { api('/api/scripts/config').then(config => { p.config = config; models(); }).catch(error => notice(error.message, true)); }
  document.addEventListener('video-ready', () => { initialProjectView = false; loadScriptConfig(); });
  document.addEventListener('video-project-view', event => {
    const project = event.detail.project;
    const changed = p.id !== project.project_id;
    if (changed) { resetProject(event.detail.preserveDraft); p.id = project.project_id; }
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
      await api(`/api/video/projects/${id}/images`, { method: 'POST', body: JSON.stringify({ request_id: p.imageRequest, prompt: $('pipelineImagePrompt').value.trim(), aspect_ratio: $('pipelineImageRatio').value, reference_ids: p.imageRefs }) });
      notice('生图任务已保存。核对下方积分后点击“生成图片”。'); if (p.id === id) await reload();
    });
  });
  $('pipelineImageReferenceFiles').addEventListener('change', async event => {
    const input = event.target, files = [...input.files];
    if (!files.length) return;
    input.disabled = true;
    try {
      if (files.length > 9 || files.some(f => f.size > 20 * 1024 * 1024) || files.reduce((n, f) => n + f.size, 0) > 40 * 1024 * 1024) throw new Error('最多上传 9 张，单张 20 MB，本次合计 40 MB');
      const id = await ensurePipeline();
      const form = new FormData(); for (const file of files) form.append('files', file);
      const result = await api(`/api/video/projects/${id}/assets`, { method: 'POST', body: form });
      if (p.id === id) { p.imageRefs = [...new Set([...p.imageRefs, ...result.assets.map(a => a.asset_id)])].slice(0, 8); p.imageRequest = uuid(); p.refSignature = ''; await refresh(); }
      notice('参考图已上传并勾选，可用于本次生图，也可用于后续视频。');
    } catch (error) { notice(error.message, true); } finally { input.disabled = false; input.value = ''; }
  });
  $('pipelineScriptType').addEventListener('change', templates);
  $('pipelineScriptModel').addEventListener('change', efforts);
  $('pipelineScriptForm').addEventListener('input', event => { if (event.target !== $('pipelineScriptWarningsAccepted')) invalidateQuote(); });
  $('pipelineScriptForm').addEventListener('change', event => { if (event.target !== $('pipelineScriptWarningsAccepted')) invalidateQuote(); });
  $('pipelineScriptQuote').addEventListener('click', event => busy(event.currentTarget, async () => {
    if (!$('pipelineScriptRequest').value.trim()) throw new Error('请填写脚本创作需求');
    await ensurePipeline();
    const input = body(), id = p.id;
    if (!input.input_request) throw new Error('请填写脚本创作需求');
    p.quote = null; $('pipelineScriptGenerate').disabled = true;
    const result = await api('/api/scripts/quote', { method: 'POST', body: JSON.stringify(input) });
    if (p.id !== id || JSON.stringify(body()) !== JSON.stringify(input)) return;
    p.quote = result; p.quoteBody = JSON.stringify(input); window.IWStoryboard?.showQuote('pipeline', result.storyboard_quote);
    notice('');
    $('pipelineScriptPrice').textContent = `TXT 阶段：${result.display_name} / ${result.thinking_level} · 本次 ${result.price} 积分${result.shortfall ? ' · 积分不足，还差 ' + result.shortfall : ''}`;
    $('pipelineScriptWarning').hidden = !result.warnings.length; $('pipelineScriptWarning').querySelector('span').textContent = result.warnings.join(' ') + ' 我已了解，仍按当前模型生成。';
    $('pipelineScriptWarningsAccepted').checked = false;
    $('pipelineScriptGenerate').textContent = `生成 TXT · ${result.price} 积分`;
    $('pipelineScriptGenerate').disabled = !result.can_generate || result.warnings.length > 0;
  }));
  $('pipelineScriptWarningsAccepted').addEventListener('change', () => { $('pipelineScriptGenerate').disabled = !p.quote?.can_generate || (p.quote.warnings.length > 0 && !$('pipelineScriptWarningsAccepted').checked); });
  $('pipelineScriptForm').addEventListener('submit', event => {
    event.preventDefault(); busy(event.submitter, async () => {
      if (!p.quote || JSON.stringify(body()) !== p.quoteBody) throw new Error('请先查看当前需求的报价');
      const id = p.id;
      await api('/api/scripts/tasks', { method: 'POST', body: JSON.stringify({ ...body(), request_id: p.scriptRequest, quote_signature: p.quote.quote_signature, warnings_confirmed: $('pipelineScriptWarningsAccepted').checked, storyboard_confirmed: Boolean(body().storyboard_options) }) });
      invalidateQuote(); notice('脚本已提交。完成后可预览，并点击“用于当前项目”。'); if (p.id === id) await reload(); await refresh();
    }).finally(() => { if (!p.quote) $('pipelineScriptGenerate').disabled = true; });
  });
  async function uploadFile(input, stage) {
    const file = input.files[0]; if (!file) return;
    input.disabled = true;
    try {
      const limit = stage === 'script' ? 256 * 1024 : 40 * 1024 * 1024;
      if (!file.size || file.size > limit) throw new Error(stage === 'script' ? 'TXT 最大 256 KB' : 'MP4 最大 40 MB');
      const id = await ensurePipeline();
      const form = new FormData(); form.append('file', file); form.append('request_id', uuid());
      await api(`/api/video/projects/${id}/${stage === 'script' ? 'script' : 'videos'}/upload`, { method: 'POST', body: form });
      notice(stage === 'script' ? 'TXT 已保存为当前项目脚本，未扣积分。' : 'MP4 已保存，可在项目成片中预览和下载，未扣积分。');
      if (p.id === id) await refresh();
    } catch (error) { notice(error.message, true); } finally { input.disabled = false; input.value = ''; }
  }
  $('pipelineScriptUpload').addEventListener('change', event => uploadFile(event.target, 'script'));
  $('pipelineVideoUpload').addEventListener('change', event => uploadFile(event.target, 'video'));
  $('pipelineUseScript').addEventListener('click', () => {
    const text = state.project?.script;
    if (!text) return notice('请先在第二步生成或上传脚本，并选为当前项目脚本。', true);
    if (text.length > 12000) return notice('整份脚本超过单镜头 12000 字限制，请从第二步选择需要的段落填入。', true);
    if ($('videoShotPrompt').value.trim()) return notice('镜头描述已有内容。请先保留或清空，再填入当前脚本。', true);
    $('videoShotPrompt').value = text; $('videoShotPrompt').focus(); notice('已填入当前脚本，请按这一镜需要调整画面、动作与声音。');
  });
})();
