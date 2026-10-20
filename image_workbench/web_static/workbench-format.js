// Pure display functions; no application state or network access.
(() => {
  'use strict';
  function formatDate(value, compact = false) {
    if (!value) return '时间未知';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '时间未知';
    return new Intl.DateTimeFormat('zh-CN', compact ? { month: 'numeric', day: 'numeric' } : { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
  }
  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  function formatSeconds(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    if (Number(value) > 0 && Number(value) < 1) return '不足 1 秒';
    const seconds = Math.max(0, Math.round(Number(value)));
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
  function executionDuration(detail) {
    // 老服务仍可读取详情计时；新服务的显式 null 不得回退成上一次执行的耗时。
    if (Object.hasOwn(detail, 'execution_seconds')) return detail.execution_seconds;
    return detail.timings?.total_seconds ?? detail.elapsed_seconds ?? null;
  }
  function executionTimingText(detail) {
    if (detail.status === 'queued') return '等待执行 · 尚未开始计时';
    if (['generating', 'checking'].includes(detail.status)) return `${statusText(detail.status)} · 已运行 ${formatSeconds(detail.elapsed_seconds)}`;
    if (detail.status === 'uncertain') return '执行状态待确认 · 最终耗时暂不可用';
    const seconds = executionDuration(detail);
    if (seconds !== null && seconds !== undefined) return `${detail.status === 'failed' ? '执行失败' : '本次执行完成'} · 本次执行耗时 ${formatSeconds(seconds)}`;
    return detail.status === 'failed' ? '执行失败 · 耗时未记录' : '';
  }
  function statusText(status, label) {
    const names = { draft: '待开始', queued: '等待执行', generating: '生成中', checking: '检查产物', awaiting_review: '等待审核', approved: '已通过', failed: '执行失败', uncertain: '状态待确认', running: '执行中', archiving: '归档中', succeeded: '已完成', archive_failed: '归档失败' };
    if (label && !/^[a-z][a-z0-9_-]*$/i.test(label)) return label;
    if (names[status]) return names[status];
    return '状态未知';
  }
  function artifactLabel(artifact) {
    if (artifact.review_status === 'approved') return '已审核';
    if (artifact.review_status === 'pending' && (artifact.kind === 'output' || artifact.role === 'output')) return '待审核';
    const names = { output: '候选图', candidate: '候选图', input: '参考图', reference: '参考图', base: '基底图' };
    return names[artifact.kind] || names[artifact.role] || '结果图片';
  }
  function statusClass(status) {
    return ['draft', 'queued', 'generating', 'checking', 'awaiting_review', 'approved', 'failed', 'uncertain'].includes(status) ? `status-${status}` : 'status-neutral';
  }
  function modeName(operation) { return operation === 'edit' ? '图片修改' : '图片生成'; }
  window.WorkbenchFormat = Object.freeze({ formatDate, formatBytes, formatSeconds, executionDuration, executionTimingText, statusText, artifactLabel, statusClass, modeName });
})();
