// Prompt enhancement controls for the existing creation form.
// The module only edits prompt text; it never submits a generation task.
(() => {
  'use strict';

  const MODES = new Set(['standard', 'creative']);
  const RATIOS = new Set(['auto', '1:1', '3:2', '2:3']);
  const MAX_PROMPT_CHARS = 4000;
  const MAX_REFERENCES = 8;

  function clean(value) {
    return String(value == null ? '' : value).trim();
  }

  function buildRequest({ requestId, prompt, mode, operation, ratio, baseArtifact, referenceArtifacts }) {
    const text = clean(prompt);
    if (!text) return { error: '请先写下你想要的画面，再进行 AI 强化。' };
    if (text.length > MAX_PROMPT_CHARS) return { error: `创作需求不能超过 ${MAX_PROMPT_CHARS} 个字符。` };
    if (!MODES.has(mode)) return { error: '不支持的强化方式。' };
    if (operation !== 'generate' && operation !== 'edit') return { error: '不支持的任务类型。' };
    if (!/^[0-9a-f]{32}$/.test(String(requestId || ''))) return { error: '强化请求标识无效，请重试。' };

    const references = [];
    const baseId = baseArtifact && baseArtifact.artifact_id;
    if (operation === 'edit') {
      if (!baseId) return { error: '修改图片需要先选择一张基底图片，再进行 AI 强化。' };
      references.push({
        index: 1,
        role: 'edit_target',
        label: clean(baseArtifact.filename),
        usage: '作为本次修改的基底图片，只改动用户指定的内容',
        artifact_id: baseId,
      });
    }
    for (const artifact of (referenceArtifacts || [])) {
      if (!artifact || !artifact.artifact_id || artifact.artifact_id === baseId) continue;
      references.push({
        index: references.length + 1,
        role: 'other',
        label: clean(artifact.filename),
        usage: '作为补充视觉参考，不继承身份、品牌或文字',
        artifact_id: artifact.artifact_id,
      });
    }
    if (references.length > MAX_REFERENCES) return { error: `参考图片最多 ${MAX_REFERENCES} 张。` };

    const payload = {
      request_id: requestId,
      prompt: text,
      mode,
      task_type: operation,
      aspect_ratio: RATIOS.has(ratio) ? ratio : 'auto',
    };
    if (references.length) payload.references = references;
    if (operation === 'edit') payload.edit = { inherit_aspect_ratio: true };
    return { payload };
  }

  function describeUsage(response) {
    const usage = (response && response.usage) || {};
    const parts = [usage.is_mock ? '演练模型' : `模型 ${usage.model || '未知'}`];
    if (Number.isFinite(usage.calls)) parts.push(`调用 ${usage.calls} 次`);
    if (Number.isFinite(usage.input_tokens) || Number.isFinite(usage.output_tokens)) {
      parts.push(`Token ${Number.isFinite(usage.input_tokens) ? usage.input_tokens : '—'} / ${Number.isFinite(usage.output_tokens) ? usage.output_tokens : '—'}`);
    }
    if (Number.isFinite(usage.estimated_cost)) {
      parts.push(`预估成本 ${usage.estimated_cost} ${usage.currency || ''}`.trim());
    }
    return parts.join(' · ');
  }

  function create({ query, requestId, enhance, getContext, onApply, notify = () => {} }) {
    const root = query('#promptEnhancement');
    if (!root) return { reset() {}, snapshot: () => null };
    const trigger = query('#promptEnhanceButton', root);
    const modeInputs = Array.from(root.querySelectorAll('input[name="prompt-enhance-mode"]'));
    const panel = query('#promptEnhancementResult', root);
    const status = query('#promptEnhancementStatus', root);
    const meta = query('#promptEnhancementMeta', root);
    const originalText = query('#promptEnhancementOriginal', root);
    const output = query('#promptEnhancementOutput', root);
    const applyButton = query('#promptEnhancementApply', root);
    const restoreButton = query('#promptEnhancementRestore', root);
    const retryButton = query('#promptEnhancementRetry', root);
    const closeButton = query('#promptEnhancementClose', root);
    let session = null;
    let applied = null;
    let pending = false;

    function currentMode() {
      const checked = modeInputs.find(input => input.checked);
      return checked ? checked.value : 'standard';
    }

    function setStatus(text, tone = '') {
      status.textContent = text || '';
      status.classList.remove('is-success', 'is-warning', 'is-error');
      if (tone) status.classList.add(`is-${tone}`);
    }

    function setPending(value) {
      pending = value;
      trigger.disabled = value;
      trigger.textContent = value ? '正在强化…' : 'AI 强化';
      trigger.setAttribute('aria-busy', String(value));
      modeInputs.forEach(input => { input.disabled = value; });
      if (session) {
        applyButton.disabled = value;
        restoreButton.disabled = value;
        retryButton.disabled = value;
      }
    }

    function showPanel() {
      panel.hidden = false;
      applyButton.disabled = pending || !clean(output.value);
      restoreButton.disabled = pending || !session;
      retryButton.disabled = pending;
    }

    function render(response) {
      session = {
        original: response.original_prompt,
        mode: response.mode,
        taskType: response.task_type,
        status: response.status,
        source: response.source || null,
      };
      originalText.textContent = response.original_prompt;
      output.value = response.enhanced_prompt;
      const failed = response.status === 'fallback';
      const fallback = response.fallback;
      const skill = response.skill ? response.skill.name : '提示词强化';
      if (failed) {
        setStatus((fallback && fallback.message) || '强化未完成，已保留原文，可继续手动编辑。', 'warning');
      } else {
        setStatus(`已完成「${skill}」强化，可编辑后使用。`, 'success');
      }
      const notes = [];
      if (response.warnings && response.warnings.length) notes.push(...response.warnings);
      const usage = describeUsage(response);
      if (usage) notes.push(usage);
      meta.textContent = notes.join(' ');
      showPanel();
      if (failed) notify((fallback && fallback.message) || '强化未完成，已保留原文。', true);
      else notify('提示词强化完成；点击“使用强化版”后才会更新创作需求。');
    }

    async function runEnhancement() {
      if (pending) return;
      const context = getContext() || {};
      const built = buildRequest({
        requestId: requestId(),
        prompt: context.prompt,
        mode: currentMode(),
        operation: context.operation,
        ratio: context.ratio,
        baseArtifact: context.baseArtifact,
        referenceArtifacts: context.referenceArtifacts,
      });
      if (built.error) {
        setStatus(built.error, 'error');
        showPanel();
        notify(built.error, true);
        return;
      }
      setPending(true);
      setStatus('正在强化提示词，请稍候…');
      try {
        const response = await enhance(built.payload);
        render(response);
      } catch (error) {
        setStatus(error && error.message ? error.message : '提示词强化失败，请重试。', 'error');
        showPanel();
        notify(error && error.message ? error.message : '提示词强化失败，请重试。', true);
      } finally {
        setPending(false);
        showPanel();
      }
    }

    function applyResult() {
      if (pending) return;
      const text = clean(output.value);
      if (!text) {
        setStatus('强化结果为空，请先补全内容或恢复原文。', 'error');
        return;
      }
      if (text.length > MAX_PROMPT_CHARS) {
        setStatus(`强化结果超过 ${MAX_PROMPT_CHARS} 个字符，请编辑缩短后再使用。`, 'error');
        return;
      }
      const source = session && session.status === 'enhanced' ? session.source : null;
      const enhanced = session && text !== session.original;
      applied = source && source.enhancement_id && enhanced
        ? {
          original_prompt: session.original,
          prompt_source: {
            kind: 'prompt_enhancement',
            enhancement_id: source.enhancement_id,
            skill_id: source.skill_id,
            skill_version: source.skill_version,
            mode: source.mode,
            task_type: source.task_type,
          },
        }
        : null;
      onApply(text);
      setStatus('已更新创作需求；点击“开始生成”后才会生成图片。', 'success');
      notify('已更新创作需求；点击“开始生成”后才会生成图片。');
    }

    function restoreOriginal() {
      if (pending || !session) return;
      applied = null;
      onApply(session.original);
      output.value = session.original;
      setStatus('已恢复原文；点击“开始生成”后才会生成图片。', 'success');
      notify('已恢复原文。');
    }

    function reset() {
      session = null;
      applied = null;
      pending = false;
      panel.hidden = true;
      originalText.textContent = '';
      output.value = '';
      meta.textContent = '';
      setStatus('');
      trigger.disabled = false;
      trigger.textContent = 'AI 强化';
      trigger.setAttribute('aria-busy', 'false');
      modeInputs.forEach(input => { input.disabled = false; });
      applyButton.disabled = false;
      restoreButton.disabled = false;
      retryButton.disabled = false;
    }

    trigger.addEventListener('click', runEnhancement);
    retryButton.addEventListener('click', runEnhancement);
    applyButton.addEventListener('click', applyResult);
    restoreButton.addEventListener('click', restoreOriginal);
    closeButton.addEventListener('click', () => { panel.hidden = true; });
    output.addEventListener('input', () => { applyButton.disabled = pending || !clean(output.value); });
    function snapshot() {
      return applied ? { ...applied, prompt_source: { ...applied.prompt_source } } : null;
    }

    reset();
    return { reset, snapshot };
  }

  window.WorkbenchPromptEnhancement = Object.freeze({ create, buildRequest, describeUsage });
})();
