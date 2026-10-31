// Pure historical duration estimate; no application state or network access.
(() => {
  'use strict';
  const MIN_SAMPLES = 3;
  const RANGE_MIN_SPREAD_SECONDS = 8;
  const RANGE_SPREAD_RATIO = 0.25;
  const ROUNDING_STEP_SECONDS = 5;
  const SUCCESS_STATUSES = new Set(['awaiting_review', 'approved']);

  function hasTrustedTiming(task) {
    if (task.execution_timing_source === 'execution_record') return true;
    // 本地 restore 也用 job_clock，但只覆盖归档阶段；远端记录以 remote- 前缀区分。
    return task.execution_timing_source === 'job_clock'
      && String(task.execution_backend || '').startsWith('remote-');
  }

  function measuredSeconds(task) {
    if (!task || !SUCCESS_STATUSES.has(task.status)) return null;
    if (!hasTrustedTiming(task)) return null;
    const seconds = Number(task.execution_seconds);
    return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
  }

  function normalizeBackend(value) {
    return String(value || '').trim().replace(/^remote-/, '');
  }

  function matchesBackend(task, backend) {
    const expected = normalizeBackend(backend);
    return !expected || normalizeBackend(task.execution_backend) === expected;
  }

  function effectiveReferences(task) {
    const count = Number(task.input_count);
    const references = Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
    return references + (task.base_artifact_id ? 1 : 0);
  }

  function resolutionBucket(width, height) {
    const w = Number(width);
    const h = Number(height);
    if (!(Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0)) return null;
    const ratio = w / h;
    const shape = ratio > 1.1 ? 'landscape' : ratio < 0.9 ? 'portrait' : 'square';
    return `${shape}:${Math.max(w, h) > 1024 ? 'large' : 'small'}`;
  }

  function taskResolution(task) {
    const artifact = task && task.preview_artifact;
    return artifact ? resolutionBucket(artifact.width, artifact.height) : null;
  }

  function detailResolution(detail) {
    if (detail && detail.base_image) {
      const bucket = resolutionBucket(detail.base_image.width, detail.base_image.height);
      if (bucket) return bucket;
    }
    const outputs = Array.isArray(detail && detail.outputs) ? detail.outputs : [];
    const latest = outputs[outputs.length - 1];
    return latest ? resolutionBucket(latest.width, latest.height) : null;
  }

  function roundToStep(value) {
    return Math.max(1, Math.round(value / ROUNDING_STEP_SECONDS) * ROUNDING_STEP_SECONDS);
  }

  function unavailable(sampleCount) {
    return {
      available: false, seconds: null, low: null, high: null, sampleCount, basis: null,
      text: '暂无足够历史数据', source: `至少需要 ${MIN_SAMPLES} 次同类完成任务`,
    };
  }

  function describe(seconds, basis) {
    const mean = seconds.reduce((sum, value) => sum + value, 0) / seconds.length;
    const minimum = Math.min(...seconds);
    const maximum = Math.max(...seconds);
    const spread = maximum - minimum;
    const rounded = Math.max(1, Math.round(mean));
    const useRange = seconds.length > 1
      && spread >= Math.max(RANGE_MIN_SPREAD_SECONDS, mean * RANGE_SPREAD_RATIO);
    if (!useRange) {
      return {
        available: true, seconds: rounded, low: rounded, high: rounded,
        sampleCount: seconds.length, basis,
        text: `约 ${rounded} 秒`, source: `基于历史同类任务 · ${seconds.length} 次`,
      };
    }
    const low = roundToStep(minimum);
    const high = Math.max(low + ROUNDING_STEP_SECONDS, roundToStep(maximum));
    return {
      available: true, seconds: rounded, low, high,
      sampleCount: seconds.length, basis,
      text: `约 ${low}–${high} 秒`, source: `基于历史同类任务 · ${seconds.length} 次`,
    };
  }

  function estimateGenerationTime({ detail = {}, tasks = [], backend = '' } = {}) {
    const samples = (Array.isArray(tasks) ? tasks : [])
      .filter(task => task && task.task_id !== detail.task_id)
      .filter(task => task.operation === detail.operation)
      .filter(task => matchesBackend(task, backend))
      .map(task => ({ task, seconds: measuredSeconds(task) }))
      .filter(sample => sample.seconds !== null);
    if (samples.length < MIN_SAMPLES) return unavailable(samples.length);

    let chosen = samples;
    let basis = '任务类型';
    const references = effectiveReferences(detail);
    const sameReferences = samples.filter(sample => effectiveReferences(sample.task) === references);
    if (sameReferences.length >= MIN_SAMPLES) {
      chosen = sameReferences;
      basis = '任务类型与参考图数量';
    }
    const bucket = detailResolution(detail);
    if (bucket) {
      const sameResolution = chosen.filter(sample => taskResolution(sample.task) === bucket);
      if (sameResolution.length >= MIN_SAMPLES) {
        basis = chosen === sameReferences ? '任务类型、参考图数量与分辨率' : '任务类型与分辨率';
        chosen = sameResolution;
      }
    }
    return describe(chosen.map(sample => sample.seconds), basis);
  }

  window.WorkbenchEstimate = Object.freeze({ estimateGenerationTime });
})();
