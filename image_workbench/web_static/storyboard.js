/* Storyboard UI uses the existing authenticated adapters and never runs a video job. */
(() => {
  'use strict';
  const mounts = new Map();
  const $ = id => document.getElementById(id);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  const uid = () => crypto.randomUUID().replaceAll('-', '');
  const storage = (key, value) => { try { if (value === undefined) return sessionStorage.getItem(key); if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value); } catch (_) {} return null; };
  const requestIds = new Map();
  function idFor(key) { let id = requestIds.get(key) || storage(key); if (!id) { id = uid(); storage(key,id); } requestIds.set(key,id); return id; }
  function forgetRequest(id) {
    if (!id) return;
    for (const [key,value] of requestIds) if (value === id) { requestIds.delete(key); storage(key,null); }
    try { for (const key of Object.keys(sessionStorage)) if (key.startsWith('storyboard.') && storage(key) === id) storage(key,null); } catch (_) {}
  }
  function button(text, fn) { const b = node('button', text, 'secondary-button'); b.type = 'button'; b.addEventListener('click', async () => { b.disabled = true; try { await fn(); } catch (e) { const target = b.closest('.storyboard-panel')?.querySelector('[role="status"]'); if (target) target.textContent = e.message; else if (window.IWVideo) window.IWVideo.notice(e.message, true); else if ($('scriptNotice')) { $('scriptNotice').textContent=e.message; $('scriptNotice').hidden=false; $('scriptNotice').classList.add('is-error'); } } finally { if (b.isConnected) b.disabled = false; } }); return b; }
  function field(parent, label, name, initial = '', type = 'text') { const wrap = node('label', undefined, 'storyboard-field'); wrap.append(node('span', label)); const input = node(type === 'textarea' ? 'textarea' : 'input'); if (type !== 'textarea') input.type = type; input.name = name; input.setAttribute('aria-label',label); input.value = initial; input.defaultValue = initial; input.maxLength = 12000; if (type === 'number') { input.min = '1'; input.max = '3600'; input.step = '0.001'; } wrap.append(input); parent.append(wrap); return input; }
  function select(parent, label, name, choices) { const wrap = node('label', undefined, 'storyboard-field'); wrap.append(node('span', label)); const s = node('select'); s.name = name; s.setAttribute('aria-label',label); choices.forEach(([value,text]) => { const o = node('option', text); o.value = value; s.append(o); }); wrap.append(s); parent.append(wrap); return s; }
  function planner(parent) {
    const box = node('div', undefined, 'storyboard-options'); parent.append(box);
    const inputs = {};
    inputs.mode = select(box, '分镜执行方式', 'mode', [['mock','Mock · 0 积分'],['codex','Codex · 需独立价格与开通']]);
    inputs.model = select(box, '分镜模型', 'model', [['recommended','系统推荐']]);
    inputs.thinking = select(box, '分镜思考强度', 'thinking', [['','系统推荐']]);
    inputs.duration = field(box,'目标总时长（秒）','duration',60,'number');
    inputs.ratio = select(box,'分镜比例','ratio',[['16:9','16:9'],['9:16','9:16'],['1:1','1:1']]);
    inputs.video = select(box,'目标视频模型','video',[['h3','H3']]);
    for (const [key,label] of [['visual_style','视觉风格'],['characters','人物与一致性'],['scenes','场景'],['requirements','额外要求']]) inputs[key] = field(box,label,key,'','textarea');
    const hint = node('p','正在读取分镜独立配置…','field-hint'); parent.append(hint);
    inputs.model.addEventListener('change', () => { const m = inputs.config?.models?.find(m => m.model_id === inputs.model.value); inputs.thinking.replaceChildren(); const levels = m?.thinking_levels || []; for (const l of levels) { const o = node('option',`${l.display_name || l.id} · ${l.price == null ? '未定价' : l.price + ' 积分'}`); o.value = l.id; inputs.thinking.append(o); } inputs.thinking.disabled = !m; });
    inputs.load = async api => { try { const c = await api('/api/storyboards/config'); inputs.config = c; inputs.mode.options[0].disabled = c.mock_enabled !== true; inputs.mode.options[1].disabled = c.real_enabled !== true; for (const m of c.models || []) { const o = node('option',m.display_name || m.model_id); o.value = m.model_id; inputs.model.append(o); } if (c.video_models?.length) { inputs.video.replaceChildren(); for (const m of c.video_models) { const o = node('option',m.display_name || m.model_id || m.id); o.value = m.model_id || m.id; inputs.video.append(o); } } hint.textContent = c.real_enabled ? '分镜使用独立报价；提交前需查看价格。' : '真实分镜调用尚未开放。Mock 不调用真实模型，0 积分。'; } catch(e) { hint.textContent = e.message; } };
    inputs.body = () => { const b = { mode: inputs.mode.value, selection_mode: inputs.model.value === 'recommended' ? 'recommended' : 'manual', target_duration_ms: Math.round(Number(inputs.duration.value)*1000), aspect_ratio: inputs.ratio.value, video_model_id: inputs.video.value, reference_artifact_ids: [] }; if (!Number.isSafeInteger(b.target_duration_ms) || b.target_duration_ms < 1000) throw new Error('请填写有效目标时长'); if (b.selection_mode === 'manual') { b.model_id = inputs.model.value; b.thinking_level = inputs.thinking.value; } for (const k of ['visual_style','characters','scenes','requirements']) b[k] = inputs[k].value.trim(); return b; };
    return inputs;
  }
  function quoteText(q) { return q ? `分镜阶段：${q.display_name || q.model_id || ''} ${q.thinking_level || ''} · ${q.price == null ? '未定价，不可执行' : q.price + ' 积分'}${q.reason ? ' · ' + q.reason : ''}${q.shortfall ? ' · 还差 ' + q.shortfall + ' 积分' : ''}` : '分镜阶段：尚未报价'; }
  function mountScript(m) {
    const details = node('details',undefined,'scripts-options storyboard-panel'); const summary = node('summary','同时生成分镜（默认关闭）'); details.append(summary);
    const label = node('label',undefined,'storyboard-check'); const checkbox = node('input'); checkbox.type = 'checkbox'; label.append(checkbox,node('span','TXT 完成归档后自动进入独立分镜规划')); details.append(label);
    const controls = planner(details); const quote = node('p','分镜阶段：未启用','field-hint'); quote.setAttribute('role','status'); details.append(quote); m.anchor.before(details);
    m.controls = controls; m.checkbox = checkbox; m.quoteElement = quote; m.panel = details; mounts.set(m.prefix,m);
    details.addEventListener('input',()=> { quote.textContent = checkbox.checked ? '分镜阶段：参数已更改，请重新报价' : '分镜阶段：未启用'; m.invalidate(); });
    details.addEventListener('change',()=> m.invalidate());
    if (m.prefix === 'script') $('scriptSubproject')?.addEventListener('change',()=> { m.confirm?.remove(); m.resultLink?.remove(); recoverScript(m); });
    else document.addEventListener('video-project-view',()=>setTimeout(()=>recoverScript(m),0));
    // Configuration reads wait until the host has established its account and CSRF state.
    let attempts = 0; const timer = setInterval(() => { const context = m.getContext(); if (context.workspace_id) { clearInterval(timer); controls.load(m.api); recoverScript(m); } else if (++attempts > 120) clearInterval(timer); },500);
  }
  function sourceVersion(d) { return d.version_id || d.output_version_id || d.versions?.find(v => v.task_id === d.task_id)?.version_id; }
  function scriptDetail(detail) { const m = mounts.get('script'); if (!m) return; m.detail = detail; if (!m.history) { m.history = button('为当前历史版本单独规划分镜',()=>planScript(m.detail,'script')); $('scriptResultActions').append(m.history); } m.history.hidden = !detail || detail.status !== 'completed'; if (!m.stage) { m.stage=node('div',undefined,'storyboard-linked-stage'); $('scriptResultMeta').after(m.stage); } linkedStage(detail,m.stage,'script'); }
  function linkedStage(detail,parent,prefix) {
    const m=mounts.get(prefix); if (!m || !parent) return;
    const t=detail?.storyboard_task, id=detail?.storyboard_task_id || t?.task_id;
    parent.replaceChildren(); const generation=(parent._linkedStageGeneration || 0)+1; parent._linkedStageGeneration=generation; if (!id && !detail?.storyboard_options) return;
    const statuses={queued:'排队中',running:'规划中',generating:'规划中',completed:'已完成',failed:'失败',cancelled:'已取消',uncertain:'待确认'};
    const status=node('p',`分镜阶段：${t ? statuses[t.status] || t.status : '等待 TXT 归档后启动'}${t?.error ? ' · '+t.error : ''}`,'field-hint'); status.setAttribute('role','status'); parent.append(status);
    if (!t?.storyboard_id) return;
    const context=JSON.stringify(m.getContext()), board=t.storyboard_id;
    const attach=pid=>{if(parent._linkedStageGeneration!==generation || context!==JSON.stringify(m.getContext()))return;const a=node('a','查看自动生成的分镜','text-button');a.href='/video?project='+encodeURIComponent(pid)+'#pipelineVideos';parent.append(a);};
    if (t.video_project_id) attach(t.video_project_id);
    else m.api('/api/storyboards/'+encodeURIComponent(board)).then(r=>{if(r.video_project_id)attach(r.video_project_id);}).catch(err=>{if(parent.isConnected && context===JSON.stringify(m.getContext()))status.textContent+=' · '+err.message;});
  }
  async function planScript(detail,prefix) {
    const m = mounts.get(prefix); if (!m || !detail) return; const source = sourceVersion(detail); if (!source) throw new Error('此脚本缺少归档版本 ID，请先完成 TXT 归档');
    const context = m.getContext(); const signature = JSON.stringify(context); const body = { ...m.controls.body(), source_script_version: source, ...(context.video_project_id ? {video_project_id:context.video_project_id} : {}) };
    m.panel.open = true; const q = await m.api('/api/storyboards/quote',{method:'POST',body:JSON.stringify(body)}); if (JSON.stringify(m.getContext()) !== signature) return; m.quoteElement.textContent = `脚本 v${detail.output_version || ''} · ${quoteText(q)}`;
    m.confirm?.remove(); const quotedOptions = JSON.stringify(m.controls.body()); const confirm = button('确认此版本分镜报价并提交', async()=> { if (JSON.stringify(m.getContext()) !== signature || JSON.stringify(m.controls.body()) !== quotedOptions) throw new Error('参数或项目已改变，请重新报价'); const key = 'storyboard.plan.'+context.account_id+'.'+context.workspace_id+'.'+source+'.'+JSON.stringify(body); const request = idFor(key); const t = await m.api('/api/storyboards/tasks',{method:'POST',body:JSON.stringify({...body,request_id:request,quote_signature:q.quote_signature,confirmed:true})}); storage(scriptTaskKey(m), t.task_id); confirm.remove(); await pollScript(m,t.task_id,signature); }); confirm.disabled = !q.can_generate; m.confirm = confirm; m.panel.append(confirm);
  }
  function scriptTaskKey(m) { const c=m.getContext(); return 'storyboard.script-task.'+c.account_id+'.'+c.workspace_id+'.'+(c.video_project_id || c.subproject_id); }
  function recoverScript(m) { const id=storage(scriptTaskKey(m)); if (!id || !/^[0-9a-f]{32}$/.test(id)) return; const context=JSON.stringify(m.getContext()); if(m.pollContext===context && m.pollTask===id)return; m.pollContext=context;m.pollTask=id;pollScript(m,id,context).catch(err=>{if(context===JSON.stringify(m.getContext()))m.quoteElement.textContent=err.message;}); }
  async function pollScript(m,id,context) { const t=await m.api('/api/storyboards/tasks/'+id); if(context!==JSON.stringify(m.getContext()))return; m.quoteElement.textContent=`独立分镜任务：${t.status} · ${t.task_id}${t.error ? ' · '+t.error : ''}`; if(['queued','running','generating','checking'].includes(t.status)){setTimeout(()=>pollScript(m,id,context).catch(err=>{if(context===JSON.stringify(m.getContext()))m.quoteElement.textContent=err.message;}),2000);return;} m.pollTask=null;if(['failed','cancelled'].includes(t.status))forgetRequest(t.request_id);if(t.storyboard_id){const record=await m.api('/api/storyboards/'+t.storyboard_id);if(context!==JSON.stringify(m.getContext()))return;m.resultLink?.remove();if(record.video_project_id){const a=node('a','在视频工作台查看此分镜','text-button');a.href='/video?project='+encodeURIComponent(record.video_project_id)+'#pipelineVideos';m.panel.append(a);m.resultLink=a;}} }
  window.IWStoryboard = { mountScript, options: prefix => { const m = mounts.get(prefix); return m?.checkbox.checked ? m.controls.body() : null; }, showQuote: (prefix,q) => { const m=mounts.get(prefix); if (m) m.quoteElement.textContent = m.checkbox.checked ? quoteText(q) : '分镜阶段：未启用'; }, scriptDetail, planScript, linkedStage };
  let editor;
  window.IWStoryboard.assetTarget = () => {
    const e = editor;
    if (!e?.record || !e.root.offsetParent || !e.selected) return null;
    if (document.activeElement?.closest('#videoShotForm') && e.boundShot !== e.selected) return null;
    return {project_id:e.project, storyboard_id:e.record.storyboard_id, shot_id:e.selected, version:e.record.version};
  };
  window.IWStoryboard.attachAssets = (assets, target, purpose='storyboard') => {
    const e = editor;
    if (!e?.record || !target || e.project !== target.project_id || e.record.storyboard_id !== target.storyboard_id || e.selected !== target.shot_id || e.record.version !== target.version) throw new Error('分镜选择已改变；图片已保存，请在目标镜头重新选择');
    if (e.record.version !== e.latest || !e.fieldValues) throw new Error('历史分镜版本只读，请选择最新版本');
    const ids = [...new Set(assets.map(a=>a.artifact_id))];
    if (ids.some(id=>!/^[a-f0-9]{32}$/.test(id || ''))) throw new Error('图片缺少有效素材标识，请从项目素材库重新选择');
    const frame = purpose==='first' ? 'start_frame_artifact_id' : purpose==='last' ? 'end_frame_artifact_id' : null;
    if (frame && (ids.length !== 1 || (e.fieldValues[frame].value && e.fieldValues[frame].value !== ids[0]))) throw new Error('该帧已有图片；请先在分镜中取消原选择，再添加新图片');
    const selected = new Set(e.referenceChecks.filter(i=>i.checked).map(i=>i.value));
    if (!frame && new Set([...selected,...ids]).size > 9) throw new Error('每个分镜最多使用9张参考图，请先减少选择');
    for (const asset of assets) {
      if (!e.assets.some(a=>a.artifact_id===asset.artifact_id)) e.assets.push(asset);
      let cb=e.referenceChecks.find(i=>i.value===asset.artifact_id);
      if (!cb) {
        const label=node('label',undefined,'storyboard-check'); cb=node('input'); cb.type='checkbox';cb.value=asset.artifact_id;
        label.append(cb,node('span',asset.title || asset.filename || asset.artifact_id));e.referenceContainer.append(label);e.referenceChecks.push(cb);
      }
      if (!frame) cb.checked=true;
      for (const key of ['start_frame_artifact_id','end_frame_artifact_id']) {
        const select=e.fieldValues[key]; if (![...select.options].some(o=>o.value===asset.artifact_id)) { const option=node('option',asset.title || asset.filename || asset.artifact_id);option.value=asset.artifact_id;select.append(option); }
      }
    }
    if (frame) e.fieldValues[frame].value=ids[0];
    e.dirty=true;e.host.notice('图片已加入选中分镜，其他编辑已保留。点击“保存选中镜头”保存引用。');
  };
  function mountVideo() {
    if (editor || !window.IWVideo) return; const host = window.IWVideo;
    const root = node('section',undefined,'storyboard-panel'); root.append(node('h3','AI 分镜规划与审核')); const message = node('p','选择项目后读取分镜。','field-hint'); message.setAttribute('role','status'); root.append(message);
    const toolbar = node('div',undefined,'storyboard-toolbar'); root.append(toolbar); const listSelect = select(toolbar,'分镜记录','record',[]), versionSelect = select(toolbar,'历史版本（只读）','version',[]);
    const content = node('div'); root.append(content); const planning = node('details'); planning.append(node('summary','全局 / 局部 AI 重新规划（先预览）')); const controls = planner(planning); const reason = field(planning,'规划原因','reason','','textarea'); const scope = select(planning,'规划范围','scope',[['all','全局'],['selected','仅勾选镜头（支持多选）']]); const planQuote = node('p','分镜阶段：尚未报价','field-hint'); planning.append(planQuote); root.append(planning);
    const preview = node('div',undefined,'storyboard-preview'); root.append(preview); $('videoShotForm').before(root);
    editor = { host, root, message, toolbar, listSelect, versionSelect, content, planning, controls, reason, scope, planQuote, preview, project:null, record:null, assets:[], selected:null, epoch:0, loading:false, task:null, pendingQuote:null, selectedIds:new Set(), dirty:false, formDirty:false };
    const e = editor; controls.load(host.api);
    window.IWImageInput?.bind({zone:root,purpose:'storyboard'});
    content.addEventListener('input',event=>{if(!event.target.dataset.replanSelect && event.target.type !== 'checkbox')e.dirty=true;});
    content.addEventListener('change',event=>{if(!event.target.dataset.replanSelect && !event.target.dataset.draftTest)e.dirty=true;});
    $('videoShotForm').addEventListener('input',()=>{if(e.boundShot)e.formDirty=true;});
    $('videoShotForm').addEventListener('change',()=>{if(e.boundShot)e.formDirty=true;});
    e.context = host.captureContext();
    const current = token => token === e.epoch && host.isCurrent(e.context) && e.project === e.context.id;
    const post = (url,body) => host.api(url,{method:'POST',body:JSON.stringify(body)});
    const req = (action,body) => { const key = 'storyboard.request.'+host.snapshot().account?.account_id+'.'+host.snapshot().account?.workspace_id+'.'+e.project+'.'+e.record?.storyboard_id+'.'+action+'.'+JSON.stringify(body); return idFor(key); };
    const clearPreview = () => { e.pendingQuote = null; e.preview.replaceChildren(); };
    async function loadRecord(version) {
      const id = listSelect.value; if (!id) { e.record = null; content.replaceChildren(node('p','当前项目还没有 AI 分镜。可在脚本历史中选择具体版本规划。')); return; }
      const token = ++e.epoch; const r = await host.api(`/api/storyboards/${encodeURIComponent(id)}${version ? '?version='+encodeURIComponent(version) : ''}`); if (!current(token) || listSelect.value !== id) return;
      if (e.record?.storyboard_id !== r.storyboard_id || (version && Number(version) !== e.record?.version)) e.boundShot = null; e.record = r; e.dirty=false; e.formDirty=false; e.selectedIds=new Set([...e.selectedIds].filter(id=>(r.shots || []).some(s=>s.shot_id===id))); e.latest = Math.max(r.version || 0,...(r.versions || []).map(v=>v.version)); e.selected = r.shots?.some(s=>s.shot_id === e.selected) ? e.selected : r.shots?.[0]?.shot_id; versionSelect.replaceChildren(); for (const v of [...(r.versions || [{version:r.version}])].reverse()) { const o=node('option',`v${v.version} · ${v.status || ''}${v.change_reason ? ' · '+v.change_reason : ''}`); o.value=v.version; versionSelect.append(o); } versionSelect.value=r.version; clearPreview(); render();
      try { const a = await host.api(`/api/storyboards/${encodeURIComponent(id)}/assets`); if (!current(token)) return; e.assets = a.assets || []; if(!e.dirty)render(); } catch(err) { if (current(token)) message.textContent=err.message; }
      recoverTask().catch(err=>{if(current(token))message.textContent=err.message;});
    }
    async function reload(passive = false) {
      if (!e.project || e.loading) return; e.loading=true; const token=e.epoch, pid=e.project, context=e.context;
      try {
        const result=await host.api('/api/storyboards?video_project_id='+encodeURIComponent(pid)); if (!current(token)) return;
        const records=result.storyboards || [], prev=listSelect.value;
        listSelect.replaceChildren(); for (const r of records) { const o=node('option',`分镜 v${r.version} · ${r.status || ''} · ${r.storyboard_id.slice(0,8)}`);o.value=r.storyboard_id;listSelect.append(o); }
        if ([...listSelect.options].some(o=>o.value===prev)) listSelect.value=prev;
        const latest=records.find(r=>r.storyboard_id===e.record?.storyboard_id);
        if (passive && e.record) {
          const newVersion=latest && Number(latest.version)>e.latest;
          const wasLatest=e.record.version===e.latest;
          if(newVersion)e.latest=Number(latest.version);
          if(e.dirty || e.formDirty || preview.childElementCount || root.contains(document.activeElement)) {
            if(newVersion)message.textContent=`发现新分镜版本 v${latest.version}。已保留当前未保存内容与候选预览；完成编辑后点击“刷新分镜”查看。`;
            return;
          }
          if(!newVersion || !wasLatest)return;
        }
        await loadRecord();
      } finally {if(e.project===pid && host.isCurrent(context))e.loading=false;}
    }
    async function edit(operation,extra={}) { const r=e.record; if (!r || r.version !== e.latest) throw new Error('历史版本只读，请切换最新版本'); const token=e.epoch; const body={expected_version:r.version,operation,reason:'手动编辑',...extra}; await post(`/api/storyboards/${r.storyboard_id}/edit`,{...body,request_id:req(operation,body)}); if (current(token)) await loadRecord(); }
    function selected() { return e.record?.shots?.find(s=>s.shot_id===e.selected); }
    function fillVideo(s) { e.boundShot = s.shot_id; $('videoShotName').value=s.title || ''; $('videoShotPrompt').value=s.video_prompt || [s.visual_description,s.action,s.camera_framing,s.camera_movement,s.dialogue,s.voiceover].filter(Boolean).join('\n'); $('videoDuration').value=Number(s.generation_duration_ms || 5000)/1000; $('videoRatio').value={'16:9':'landscape','9:16':'portrait','1:1':'square'}[s.aspect_ratio] || 'landscape'; $('videoMode').value=s.start_frame_artifact_id ? (s.end_frame_artifact_id ? 'FL2V' : 'I2V') : s.reference_artifact_ids?.length ? 'R2V' : 'T2V'; const artifactIds = s.start_frame_artifact_id ? [s.start_frame_artifact_id,s.end_frame_artifact_id].filter(Boolean) : (s.reference_artifact_ids || []); host.setReferences(artifactIds.map(id => e.assets.find(a => a.artifact_id === id)?.video_asset_id).filter(Boolean), {context: host.captureContext(), mode: $('videoMode').value}); e.boundReferences = {mode:$('videoMode').value, ids:[...host.snapshot().referenceOrder]}; for (const input of $('videoReferenceChoices').querySelectorAll('input')) input.checked = host.snapshot().referenceOrder.includes(input.value); $('videoMode').dispatchEvent(new Event('change')); $('videoDuration').dispatchEvent(new Event('input')); e.formDirty=false; }
    function render() {
      const r=e.record; content.replaceChildren(); if (!r) return; const readonly=r.version !== e.latest; message.textContent=`v${r.version} · ${r.status} · ${r.shots?.length || 0} 镜头 · 叙事总时长 ${((r.shots || []).reduce((n,s)=>n+Number(s.narrative_duration_ms || 0),0)/1000).toFixed(2)} 秒${readonly ? ' · 历史版本只读' : ''}`;
      if (r.duration_conflict) content.append(node('p',`总时长冲突：${r.duration_conflict.reason} · 目标 ${r.duration_conflict.target_duration_ms/1000} 秒，合理时长 ${r.duration_conflict.reasonable_duration_ms/1000} 秒。${JSON.stringify(r.duration_conflict.suggestions || [])}`,'storyboard-conflict'));
      const rows=node('div',undefined,'storyboard-shots'); content.append(rows);
      for (const [i,s] of (r.shots || []).entries()) { const row=node('div',undefined,'storyboard-shot'); row.draggable=!readonly; row.dataset.shotId=s.shot_id;
        const multiLabel=node('label',undefined,'storyboard-multi-select'); const multi=node('input'); multi.type='checkbox';multi.dataset.replanSelect='true';multi.setAttribute('aria-label',`局部规划镜头 ${i+1}`);multi.checked=e.selectedIds.has(s.shot_id);multi.disabled=readonly;multi.addEventListener('change',()=>{if(multi.checked)e.selectedIds.add(s.shot_id);else e.selectedIds.delete(s.shot_id);clearPreview();planQuote.textContent=`已勾选 ${e.selectedIds.size} 个镜头，请重新报价。`;});multiLabel.append(multi);row.append(multiLabel);
        const b=button(`${i+1}. ${s.title || '镜头'} · ${s.narrative_duration_ms/1000}s / 生成 ${s.generation_duration_ms/1000}s`,()=>{e.selected=s.shot_id; fillVideo(s); render();}); b.setAttribute('aria-pressed',String(e.selected===s.shot_id)); row.append(b); if (!readonly) {row.append(button('↑',()=>reorder(s.shot_id,-1)),button('↓',()=>reorder(s.shot_id,1)));} row.addEventListener('dragstart',event=>event.dataTransfer.setData('text/plain',s.shot_id)); row.addEventListener('dragover',event=> {if (!readonly) event.preventDefault();}); row.addEventListener('drop',event=> {event.preventDefault(); if(readonly)return; const ids=r.shots.map(s=>s.shot_id), from=ids.indexOf(event.dataTransfer.getData('text/plain')),to=ids.indexOf(s.shot_id); if(from<0 || from===to)return; ids.splice(to,0,ids.splice(from,1)[0]); edit('reorder',{shot_ids:ids}).catch(err=>message.textContent=err.message);}); if (!readonly && window.IWAssetActions) window.IWAssetActions.dropzone(row, async asset=>{
          if(asset.media_type!=='image' || asset.project_id!==e.project || asset.workspace_id!==host.snapshot().account.workspace_id || asset.subproject_id!==r.subproject_id) throw new Error('只能引用当前项目和工作空间的图片');
          if(e.dirty && e.selected!==s.shot_id) throw new Error('请先保存当前分镜的编辑，再向其他镜头添加素材');
          if(e.selected!==s.shot_id) {e.selected=s.shot_id;fillVideo(s);render();} else e.boundShot=s.shot_id;
          window.IWStoryboard.attachAssets([e.assets.find(a=>a.artifact_id===asset.artifact_id) || asset],window.IWStoryboard.assetTarget());
        },host.notice); rows.append(row); }
      const s=selected(); if (!s) { if (!readonly) content.append(button('添加镜头',()=>edit('add',{changes:{title:'新镜头',narrative_duration_ms:5000,generation_duration_ms:5000,video_model_id:'h3',aspect_ratio:'16:9'}}))); return; }
      if(s.duration_compatibility?.message) content.append(node('p',s.duration_compatibility.message,s.duration_compatibility.supported === false ? 'storyboard-conflict' : 'field-hint'));
      const fields=node('div',undefined,'storyboard-options'); const values={}; for(const [k,label] of [['title','标题'],['scene_id','场景 ID'],['visual_description','画面'],['action','动作'],['camera_framing','景别'],['camera_movement','运镜'],['emotion','情绪'],['dialogue','对白'],['voiceover','旁白'],['image_prompt','图片提示词'],['video_prompt','视频提示词'],['duration_reason','时长依据'],['aspect_ratio','比例'],['video_model_id','视频模型']]) values[k]=field(fields,label,k,s[k] || '',['visual_description','video_prompt','image_prompt','dialogue','voiceover'].includes(k)?'textarea':'text'); values.narrative_duration_ms=field(fields,'叙事时长（秒）','narrative_duration_ms',s.narrative_duration_ms/1000,'number'); values.generation_duration_ms=field(fields,'生成时长（秒）','generation_duration_ms',s.generation_duration_ms/1000,'number');
      const refs=node('div',undefined,'storyboard-options'); fields.append(refs); const checked=[]; for (const a of e.assets) { const label=node('label',undefined,'storyboard-check');const cb=node('input');cb.type='checkbox';cb.checked=(s.reference_artifact_ids || []).includes(a.artifact_id);cb.value=a.artifact_id;label.append(cb,node('span',a.title || a.filename || a.artifact_id));refs.append(label);checked.push(cb); }
      for(const k of ['start_frame_artifact_id','end_frame_artifact_id']) {const choices=[['','无'],...e.assets.map(a=>[a.artifact_id,a.title || a.filename || a.artifact_id])]; if(s[k] && !e.assets.some(a=>a.artifact_id===s[k])) choices.push([s[k],'素材缺失 · '+s[k]]); values[k]=select(fields,k==='start_frame_artifact_id'?'首帧 Artifact':'尾帧 Artifact',k,choices);values[k].value=s[k] || '';}
      e.fieldValues=values;e.referenceContainer=refs;e.referenceChecks=checked;
      const missing=(s.reference_artifact_ids || []).filter(id=>!e.assets.some(a=>a.artifact_id===id && a.available!==false)); if(missing.length) fields.append(node('p','参考图缺失，保存时保留原引用：'+missing.join('、'),'storyboard-conflict'));
      for(const input of fields.querySelectorAll('input,textarea,select')) input.disabled=readonly; content.append(fields); const actions=node('div',undefined,'storyboard-toolbar');content.append(actions);
      if(!readonly) { actions.append(button('保存选中镜头',()=>{const changes={};for(const [k,input] of Object.entries(values)) changes[k]=k.endsWith('_duration_ms')?Math.round(Number(input.value)*1000):input.value; changes.reference_artifact_ids=[...checked.filter(i=>i.checked).map(i=>i.value),...missing];return edit('update',{shot_id:s.shot_id,changes});}),button('添加镜头',()=>edit('add',{changes:{title:'新镜头',narrative_duration_ms:5000,generation_duration_ms:5000,video_model_id:s.video_model_id,aspect_ratio:s.aspect_ratio}})),button('删除选中镜头',()=>edit('delete',{shot_id:s.shot_id})),button('均分为两镜头',()=>edit('split',{shot_id:s.shot_id,parts:[{narrative_duration_ms:Math.floor(s.narrative_duration_ms/2)},{narrative_duration_ms:s.narrative_duration_ms-Math.floor(s.narrative_duration_ms/2)}]})));
      const next=r.shots[r.shots.indexOf(s)+1]; if(next)actions.append(button('与后一镜头合并',()=>edit('merge',{shot_ids:[s.shot_id,next.shot_id]}))); const target=field(content,'保存目标总时长（秒）','target',r.target_duration_ms/1000,'number');actions.append(button('保存分镜版本',()=>edit('save',{target_duration_ms:Math.round(Number(target.value)*1000)})),button('审核通过',()=>edit('approve')),button('退回修改',()=>edit('revision_required'))); }
      const draftLabel=node('label',undefined,'storyboard-check');const test=node('input');test.type='checkbox';test.dataset.draftTest='true';draftLabel.append(test,node('span','明确允许未审核草稿的单镜头测试'));content.append(draftLabel);
      const draft=button('创建绑定的视频草稿 · 不执行生成',async()=>{const refs=[...(s.reference_artifact_ids || []),s.start_frame_artifact_id,s.end_frame_artifact_id].filter(Boolean);if(refs.some(id=>!e.assets.some(a=>a.artifact_id===id && a.available!==false)))throw new Error('引用资产缺失或不可用，请先修复分镜引用。');if(r.status!=='approved'&&!test.checked)throw new Error('未审核版本需明确勾选草稿单镜头测试'); const token=e.epoch, pid=e.project; const body={expected_version:r.version,resolution:$('videoResolution').value,allow_draft_test:test.checked};await post(`/api/storyboards/${r.storyboard_id}/shots/${s.shot_id}/video-draft`,{...body,request_id:req('video-draft:'+s.shot_id,body)});if(!current(token))return; await host.refresh();host.notice('已创建关联视频草稿。请在下方核对该镜头积分，再点击原“生成视频”按钮。');if(e.project!==pid)return;}); draft.disabled=s.duration_compatibility?.supported === false; draft.dataset.storyboardDraft = 'true'; content.append(draft);
      for(const video of r.videos || []) content.append(node('p',`${video.storyboard_shot_id || video.shot_id} · ${video.status || ''}${video.outdated ? ' · 内容已修改，视频已过期' : ''}`,'field-hint'));
    }
    function reorder(id,delta) {const ids=e.record.shots.map(s=>s.shot_id),i=ids.indexOf(id),j=i+delta;if(j<0||j>=ids.length)return;[ids[i],ids[j]]=[ids[j],ids[i]];return edit('reorder',{shot_ids:ids});}
    toolbar.append(button('刷新分镜',()=>reload()),button('解除镜头带入，独立手动视频',()=>{e.boundShot=null; e.formDirty=false; host.notice('已解除 AI 镜头带入；原视频表单恢复独立保存。');})); listSelect.addEventListener('change',()=>loadRecord().catch(err=>message.textContent=err.message)); versionSelect.addEventListener('change',()=>loadRecord(versionSelect.value).catch(err=>message.textContent=err.message));
    function replanBody() {if(!e.record || e.record.version!==e.latest)throw new Error('请先选择最新分镜版本');if(scope.value==='selected'&&!e.selectedIds.size)throw new Error('请勾选至少一个需要局部规划的镜头');return {...controls.body(),expected_version:e.record.version,shot_ids:scope.value==='selected'?e.record.shots.filter(s=>e.selectedIds.has(s.shot_id)).map(s=>s.shot_id):[],reason:reason.value.trim(),video_project_id:e.project};}
    planning.addEventListener('input',clearPreview);planning.addEventListener('change',clearPreview);
    planning.append(button('查看重新规划报价',async()=>{const body=replanBody(),token=e.epoch;const q=await post(`/api/storyboards/${e.record.storyboard_id}/replan/quote`,body);if(!current(token)||JSON.stringify(replanBody())!==JSON.stringify(body))return;planQuote.textContent=quoteText(q);const start=button('确认报价，生成候选预览',async()=>{if(!current(token)||JSON.stringify(replanBody())!==JSON.stringify(body))throw new Error('参数已改变，请重新报价');const key=taskKey();const t=await post(`/api/storyboards/${e.record.storyboard_id}/replan`,{...body,request_id:req('replan',body),quote_signature:q.quote_signature,confirmed:true});storage(key,t.task_id);if(!current(token))return;e.task=t;await pollTask(t.task_id,token);});start.disabled=!q.can_generate;e.preview.replaceChildren(start);}));
    function taskKey(){return 'storyboard.preview.'+host.snapshot().account?.account_id+'.'+host.snapshot().account?.workspace_id+'.'+e.project+'.'+e.record?.storyboard_id;}
    async function recoverTask(){
      const key=taskKey(); let id=storage(key);
      if(!id) {
        // Recover the previous project-scoped cache only for its owning board.
        const legacy=key.slice(0,key.lastIndexOf('.')), old=storage(legacy), token=e.epoch;
        if(old && /^[0-9a-f]{32}$/.test(old)) { const task=await host.api('/api/storyboards/tasks/'+old); if(!current(token))return; if(task.storyboard_id===e.record?.storyboard_id) {id=old;storage(key,id);storage(legacy,null);} }
      }
      if(id && /^[0-9a-f]{32}$/.test(id))await pollTask(id,e.epoch);
    }
    async function pollTask(id,token){
      const key=taskKey(), t=await host.api('/api/storyboards/tasks/'+id);
      if(!current(token) || storage(key)!==id || (t.storyboard_id && t.storyboard_id!==e.record?.storyboard_id))return;
      e.task=t;const candidate=t.preview;preview.replaceChildren(node('p',`规划任务：${t.status}${t.error ? ' · '+t.error : ''}`));
      if(candidate){
        preview.append(node('pre',JSON.stringify(candidate,null,2),'storyboard-preview-text'));
        preview.append(button('接受此候选，保存为新版本',async()=>{if(!current(token))return;const body={task_id:id,expected_version:e.record.version};await post(`/api/storyboards/${e.record.storyboard_id}/accept`,{...body,request_id:req('accept',body)});storage(key,null);forgetRequest(t.request_id);if(current(token))await loadRecord();}),button('放弃此候选',()=>{if(!current(token))return;storage(key,null);forgetRequest(t.request_id);preview.replaceChildren();}));
      }else if(['queued','running','generating','checking'].includes(t.status))setTimeout(()=>pollTask(id,token).catch(err=>{if(current(token))message.textContent=err.message;}),2000);
      else if(['failed','cancelled'].includes(t.status))forgetRequest(t.request_id);
    }
    // Route the original form's save action through the bound draft endpoint when an AI shot is selected.
    $('videoShotForm').addEventListener('submit', async event => {
      if (!e.record || !selected() || e.boundShot !== e.selected) return;
      event.preventDefault(); event.stopImmediatePropagation();
      try {
        const s = selected();
        const prompt = $('videoShotPrompt').value.trim(), title = $('videoShotName').value.trim(), duration = Math.round(Number($('videoDuration').value)*1000);
        const aspect_ratio={landscape:'16:9',portrait:'9:16',square:'1:1'}[$('videoRatio').value];
        const mode=$('videoMode').value, refs=mode==='T2V'?[]:[...host.snapshot().referenceOrder];
        const refsChanged=mode!==e.boundReferences?.mode || (mode!=='T2V' && JSON.stringify(refs)!==JSON.stringify(e.boundReferences?.ids));
        const changes={title,video_prompt:prompt,generation_duration_ms:duration,aspect_ratio};
        if(refsChanged) {
          const original=[...(s.reference_artifact_ids || []),s.start_frame_artifact_id,s.end_frame_artifact_id].filter(Boolean);
          if(original.some(id=>!e.assets.some(a=>a.artifact_id===id && a.video_asset_id && a.available!==false)))throw new Error('镜头包含缺失或尚未带入视频区的资产，请先在上方分镜编辑区明确调整引用。');
          if(mode==='I2V' && refs.length!==1)throw new Error('首帧模式需要选择 1 张参考图');
          if(mode==='FL2V' && refs.length!==2)throw new Error('首尾帧模式需要选择 2 张参考图，依次为首帧和尾帧');
          if(mode==='R2V' && (refs.length<1 || refs.length>9))throw new Error('参考素材模式需要选择 1–9 张参考图');
          const artifacts=refs.map(id=>{const asset=e.assets.find(a=>a.video_asset_id===id && a.available!==false);if(!asset)throw new Error('选中的参考图缺少可用 Artifact，请刷新分镜后重新选择。');return asset.artifact_id;});
          changes.reference_artifact_ids=mode==='R2V'?artifacts:[];
          changes.start_frame_artifact_id=['I2V','FL2V'].includes(mode)?artifacts[0]:'';
          changes.end_frame_artifact_id=mode==='FL2V'?artifacts[1]:'';
        }
        if (refsChanged || aspect_ratio!==s.aspect_ratio || title !== (s.title || '') || prompt !== (s.video_prompt || [s.visual_description,s.action,s.camera_framing,s.camera_movement,s.dialogue,s.voiceover].filter(Boolean).join('\n')) || duration !== s.generation_duration_ms) {
          if (e.record.version !== e.latest) throw new Error('历史版本只读；请选择最新版本后修改镜头');
          await edit('update',{shot_id:s.shot_id,changes});
          if(e.boundShot===s.shot_id && selected()?.shot_id===s.shot_id)fillVideo(selected());
          message.textContent = '原视频表单的修改已保存为新分镜版本；请核对内容并明确选择审核或草稿测试，再创建绑定视频草稿。';
          return;
        }
        content.querySelector('[data-storyboard-draft]')?.click();
      } catch (err) { message.textContent = err.message; }
    }, true);
    document.addEventListener('video-project-view',event=>{const pid=event.detail.project.project_id;if(pid===e.project && host.isCurrent(e.context)){reload(true).catch(err=>message.textContent=err.message);return;}e.context=host.captureContext();e.project=pid;e.epoch++;e.record=null;e.selected=null;e.boundShot=null;e.selectedIds.clear();e.dirty=false;e.formDirty=false;e.assets=[];e.loading=false;content.replaceChildren();preview.replaceChildren();listSelect.replaceChildren();versionSelect.replaceChildren();reload().catch(err=>message.textContent=err.message);});
    document.addEventListener('video-project-cleared',()=>{e.project=null;e.epoch++;e.record=null;e.boundShot=null;content.replaceChildren();preview.replaceChildren();listSelect.replaceChildren();versionSelect.replaceChildren();message.textContent='选择项目后读取分镜。';});
    if(host.snapshot().project){e.project=host.snapshot().project.project_id;reload().catch(err=>message.textContent=err.message);}
  }
  document.addEventListener('video-ready',mountVideo);
})();
