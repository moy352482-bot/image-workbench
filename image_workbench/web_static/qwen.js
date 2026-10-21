(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {csrf:'', workspace:'', account:null, jobs:[], selected:null, files:[null,null], previews:[null,null], requestId:null, requestSeed:null, refreshing:false, jobsSignature:'', resultSignature:''};
  const labels = {queued:'排队中', running:'生成中', completed:'已完成', failed:'失败', uncertain:'状态待确认', cancelled:'已取消'};
  const modes = {generate:'文生图', edit:'单图编辑', reference:'双图参考'};
  const uid = () => crypto.randomUUID().replaceAll('-', '');
  const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
  async function api(path, options={}) {
    const headers = {'X-Workspace-ID':state.workspace, ...options.headers};
    if (options.method && options.method !== 'GET') headers['X-Workbench-CSRF'] = state.csrf;
    const response = await fetch(path, {...options, headers});
    if (response.status === 401) { location.assign('/login'); throw new Error('请重新登录'); }
    if (!response.ok) { let error; try { error = await response.json(); } catch (_) {} throw new Error(error?.detail || '连接暂不可用，请稍后刷新'); }
    return response.json();
  }
  function message(text, error=false) { $('qwenMessage').textContent = text; $('qwenMessage').classList.toggle('qwen-error', error); }
  function imageUrl(job, name, download=false) { return `/api/qwen/jobs/${job.job_id}/images/${name}?workspace_id=${encodeURIComponent(job.workspace_id)}${download ? '&download=true' : ''}`; }
  function status(job) { return job.status === 'running' && !job.started_at ? '等待本机开始' : labels[job.status] || job.status; }
  function setFile(index, file) {
    if (file && file.size > 20*1024*1024) throw new Error('单张图片不能超过 20 MB');
    state.files[index] = file; state.requestId = null;
    if (state.previews[index]) URL.revokeObjectURL(state.previews[index]);
    state.previews[index] = file ? URL.createObjectURL(file) : null;
    const preview = $(index ? 'qwenReferencePreview' : 'qwenInputPreview'); preview.replaceChildren();
    if (file) { const img = el('img'); img.src = state.previews[index]; img.alt = index ? '第二张参考图预览' : '原图预览'; preview.append(img, el('span','',`${file.name} · ${(file.size/1024/1024).toFixed(2)} MB`)); }
  }
  function modeChanged() {
    const mode = $('qwenMode').value;
    $('qwenInputGroup').hidden = mode === 'generate'; $('qwenReferenceGroup').hidden = mode !== 'reference';
    $('qwenResolution').disabled = mode !== 'generate'; if (mode !== 'generate') $('qwenResolution').value = '1024';
    $('qwenModeHint').textContent = mode === 'generate' ? '从需求生成一张方形图片，可测试 1024 或 1536。' : mode === 'reference' ? '第一张是待编辑原图，第二张提供参考；指令中可用 <image1> 和 <image2> 指定。' : '每轮只修改一处，描述要保留的商品与构图。输出画幅随原图，分辨率档位为 1024。';
    state.requestId = null;
  }
  function renderResult() {
    const job = state.jobs.find(j => j.job_id === state.selected); if (!job) return;
    const signature = JSON.stringify({...job,heartbeat_at:null});
    if (signature === state.resultSignature) return; state.resultSignature = signature;
    const root = $('qwenResult'); root.replaceChildren();
    $('qwenResultStatus').textContent = status(job); $('qwenResultStatus').dataset.status = job.status;
    const images = el('div','qwen-images');
    const add = (name, caption) => { const figure = el('figure'), link = el('a'), img = el('img'); link.href = imageUrl(job,name); link.target = '_blank'; link.rel = 'noopener'; img.src = link.href; img.alt = caption; link.append(img); figure.append(el('figcaption','',caption),link); images.append(figure); };
    for (const input of job.inputs || []) add(`input-${input.index}`, input.index === 1 ? '原图' : '第二张参考图');
    if (job.status === 'completed' && job.output) add('output','模型生成结果');
    if (images.childElementCount) root.append(images);
    if (!job.output) { const empty = el('div','qwen-empty'); empty.append(el('h3','',status(job)),el('p','',job.error || (job.status === 'queued' ? '任务已保存在网站，执行器在线后依次处理。' : job.status === 'running' ? '本机正在处理，完成后图片会自动回传。' : '可调整需求后新建一次测试。'))); root.append(empty); }
    root.append(el('p','qwen-detail-prompt',job.task.prompt));
    const meta = `${modes[job.task.mode]} · ${job.task.model.toUpperCase()} · 种子 ${job.task.seed} · ${job.output ? `${job.output.width} × ${job.output.height}` : job.task.resolution}`;
    root.append(el('p','qwen-result-meta',meta + (job.elapsed_seconds != null ? ` · 模型执行与取图 ${job.elapsed_seconds.toFixed(1)} 秒` : '')));
    const actions = el('div','qwen-result-actions');
    if (job.status === 'completed') {
      const download = el('a','primary-button','下载 PNG'); download.href = imageUrl(job,'output',true); actions.append(download);
      const edit = el('button','secondary-button','用这张图继续编辑'); edit.type = 'button'; edit.addEventListener('click', async () => { try { const response = await fetch(imageUrl(job,'output')); if (!response.ok) throw new Error('读取成图失败'); setFile(0,new File([await response.blob()],`qwen-${job.job_id}.png`,{type:'image/png'})); $('qwenMode').value = 'edit'; modeChanged(); $('qwenSeed').value = ''; $('qwenPrompt').value = ''; $('qwenPrompt').focus(); message('已选为原图，将使用新的随机种子。请填写编辑指令'); } catch (e) { message(e.message,true); } }); actions.append(edit);
    }
    if (job.status === 'queued') { const cancel = el('button','secondary-button','取消排队'); cancel.type = 'button'; cancel.addEventListener('click', async () => { try { await api(`/api/qwen/jobs/${job.job_id}/cancel`,{method:'POST'}); await refresh(); } catch(e) { message(e.message,true); } }); actions.append(cancel); }
    root.append(actions);
  }
  function renderHistory() {
    const list = $('qwenHistory'); list.replaceChildren(); list.className = 'qwen-history-list';
    if (!state.jobs.length) { list.append(el('p','qwen-help','还没有测试任务。')); return; }
    if (!state.jobs.some(j => j.job_id === state.selected)) state.selected = state.jobs[0].job_id;
    for (const job of state.jobs) {
      const button = el('button','qwen-history-item' + (job.job_id === state.selected ? ' selected' : '')); button.type = 'button';
      if (job.status === 'completed') { const img = el('img'); img.src = imageUrl(job,'output'); img.alt = ''; img.loading = 'lazy'; button.append(img); }
      const text = el('div'); text.append(el('strong','',job.task.prompt),el('small','',`${modes[job.task.mode]} · ${job.task.model.toUpperCase()} · ${new Date(job.created_at).toLocaleString()}`));
      const badge = el('span','qwen-status',status(job)); badge.dataset.status = job.status; button.append(text,badge);
      button.addEventListener('click',() => {state.selected = job.job_id; renderHistory(); renderResult();}); list.append(button);
    }
    renderResult();
  }
  function workerView(config) {
    const w = config.worker; $('qwenWorker').textContent = w.online && w.ready ? '本机执行器在线' : w.online ? '模型暂不可用' : '本机执行器离线';
    $('qwenWorker').classList.toggle('qwen-worker-ready',w.online && w.ready);
    const text = !w.online ? '本机执行器离线：仍可提交排队，电脑和执行器在线后会处理。' : !w.ready ? w.message || '本机模型暂不可用，已提交任务会保留。' : w.message;
    $('qwenNotice').hidden = !text; $('qwenNotice').textContent = text;
  }
  async function refresh() {
    if (state.refreshing) return; state.refreshing = true;
    try { const [config,result] = await Promise.all([api('/api/qwen/config'),api('/api/qwen/jobs')]); state.csrf = config.csrf_token; state.jobs = result.jobs; workerView(config); const signature = JSON.stringify(result.jobs.map(j => ({...j,heartbeat_at:null}))); if (signature !== state.jobsSignature) { state.jobsSignature = signature; renderHistory(); } }
    finally { state.refreshing = false; }
  }
  $('qwenMode').addEventListener('change',modeChanged);
  $('qwenForm').addEventListener('input',() => {state.requestId = null;});
  for (const [index,id] of ['qwenInput','qwenReference'].entries()) $(id).addEventListener('change',event => { try { setFile(index,event.target.files[0] || null); message(''); } catch(e) { event.target.value = ''; setFile(index,null); message(e.message,true); } });
  $('qwenRandom').addEventListener('click',() => { $('qwenSeed').value = crypto.getRandomValues(new Uint32Array(1))[0]; state.requestId = null; });
  const presets = {price:['edit','Edit <image1>. Change only the price from "199" to "299". Preserve all other text, the product, background and layout. Do not add any text.'], label:['edit','Edit <image1>. Erase only the small printed letters from the product label. Keep the paper label, its border, shading and position intact. Preserve the product and all text outside the label.'], poster:['generate','一张简洁的咖啡馆海报，奶油色背景，中央是一杯拿铁，旁边一枝绿叶，柔和自然光。顶部大字“慢享时光”，底部小字“COFFEE & LIFE”，高品质商业摄影。']};
  document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click',() => { const [mode,prompt] = presets[button.dataset.preset]; $('qwenMode').value = mode; $('qwenPrompt').value = prompt; modeChanged(); }));
  $('qwenForm').addEventListener('submit',async event => {
    event.preventDefault(); if (!$('qwenForm').reportValidity()) return;
    const mode = $('qwenMode').value, count = {generate:0,edit:1,reference:2}[mode];
    if (state.files.slice(0,count).some(file => !file)) { message(count === 2 ? '请上传原图和第二张参考图' : '请先上传待编辑原图',true); return; }
    if (!state.requestId) {
      state.requestId = uid();
      // Keep the sampled seed with its request ID so a lost-response retry stays identical.
      state.requestSeed = $('qwenSeed').value === '' ? crypto.getRandomValues(new Uint32Array(1))[0] : Number($('qwenSeed').value);
    }
    const data = new FormData();
    data.append('payload',JSON.stringify({request_id:state.requestId,mode,model:$('qwenModel').value,prompt:$('qwenPrompt').value.trim(),seed:state.requestSeed,resolution:Number($('qwenResolution').value)}));
    for (const file of state.files.slice(0,count)) data.append('images',file);
    $('qwenSubmit').disabled = true; message('正在上传需求和图片…');
    try { const job = await api('/api/qwen/jobs',{method:'POST',body:data}); state.selected = job.job_id; message('已提交，结果会自动显示'); try { await refresh(); } catch (_) { message('任务已保存，状态刷新暂不可用，请稍后点击刷新。'); } }
    catch(e) { message(e.message + '；重试同一提交不会重复生成。',true); }
    finally { $('qwenSubmit').disabled = false; }
  });
  $('qwenRefresh').addEventListener('click',() => refresh().catch(e => message(e.message,true)));
  $('qwenWorkspace').addEventListener('change',async () => { state.workspace = $('qwenWorkspace').value; state.selected = null; state.requestId = null; state.jobsSignature = ''; state.resultSignature = ''; $('qwenResult').replaceChildren(); $('qwenResultStatus').textContent = '尚未选择任务'; try { await refresh(); } catch(e) { message(e.message,true); } });
  $('qwenLogout').addEventListener('click',async () => { try { await api('/api/auth/logout',{method:'POST'}); location.assign('/login'); } catch(e) { message(e.message,true); } });
  async function init() {
    const auth = await api('/api/auth/config'); state.csrf = auth.csrf_token;
    state.account = await api('/api/auth/me'); state.workspace = state.account.workspace_id;
    $('qwenAccount').textContent = `${state.account.username} · 管理员`;
    for (const ws of state.account.workspaces) { const option = el('option','',ws.name); option.value = ws.workspace_id; $('qwenWorkspace').append(option); }
    $('qwenWorkspace').value = state.workspace; modeChanged(); await refresh();
    setInterval(() => { if (!document.hidden) refresh().catch(e => message(e.message,true)); },3000);
  }
  init().catch(e => { message(e.message,true); $('qwenSubmit').disabled = true; });
})();
