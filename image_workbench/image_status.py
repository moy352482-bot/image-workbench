"""Image execution labels and timing; independent of storage and HTTP."""
from datetime import datetime, timezone
import math

STATUS_LABELS = {"draft": "待开始", "queued": "等待执行", "generating": "生成中", "checking": "检查产物",
                 "awaiting_review": "等待审核", "approved": "已通过", "failed": "执行失败", "uncertain": "状态待确认"}
FAILURE_MESSAGES = {
    "image_api_failed": "图片 API 请求失败，请核查接口配置和执行日志后手动重试。",
    "image_api_timeout": "图片 API 请求超时，上游完成状态未知；不会自动重新生成。",
    "prepare_failed": "输入或任务准备失败，请检查图片和提示词。",
    "codex_launch_failed": "无法启动本地 Codex，请检查本地安装和登录状态。",
    "codex_timeout": "本次 Codex 执行超时，日志已保留；不会自动重新生成。",
    "codex_execution_failed": "Codex 未完成图片生成或未提供可登记产物，请核查本地日志后手动重试。",
    "artifact_not_found": "未找到本次生成的图片文件，执行记录已保留。",
    "artifact_invalid": "返回图片未通过文件校验，未将其作为有效结果。",
    "register_failed": "图片登记失败，候选文件保留；可尝试仅恢复归档。",
    "state_update_failed": "执行记录写入失败，已保留可用证据，请先检查本地存储。",
    "interrupted": "执行状态需要确认；不会自动重放或再次消耗额度。",
    "unexpected_exception": "执行遇到异常，请查看本地日志；系统不会自动重试。",
}


def _seconds_between(start, end):
    if not start or not end:
        return None
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _execution_timing(status, latest, job):
    """列表与详情共用本次执行计时；任务创建与排队时间不算执行耗时。"""
    value, source = None, None
    if status in {"generating", "checking"}:
        start = job.get("started_at") if job else (latest.created_at if latest else None)
        value = _seconds_between(start, datetime.now(timezone.utc).isoformat())
        source = "running" if value is not None else None
    elif status not in {"draft", "queued", "uncertain"}:
        matching = latest and (not job or job.get("execution_id") == latest.execution_id)
        # 远端记录只计服务端归档，恢复归档记录则累计了先前尝试；两者采用本次作业时钟。
        use_record = matching and not latest.backend.startswith("remote-") and not (job and job.get("action") == "restore")
        if use_record and isinstance(latest.total_seconds, (int, float)) and math.isfinite(latest.total_seconds) and latest.total_seconds >= 0:
            value, source = latest.total_seconds, "execution_record"
        elif job:
            value = _seconds_between(job.get("started_at"), job.get("finished_at"))
            source = "job_clock" if value is not None else None
    return {"execution_seconds": value, "execution_timing_source": source, "elapsed_seconds": value}
