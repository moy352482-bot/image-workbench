"""Read-only task diagnostics shared by image, script and video projections.

This is a response contract, not a scheduler. Existing services alone authorize
and execute the listed actions, rechecking the current job and evidence.
"""

STATUSES = {"generating": "running", "checking": "running", "archiving": "running",
            "awaiting_review": "completed", "approved": "completed", "succeeded": "completed",
            "credit_insufficient": "blocked"}
STAGES = {"draft": "prepare", "queued": "queue", "running": "execute", "generating": "execute",
          "checking": "archive", "archiving": "archive", "awaiting_review": "review", "approved": "done",
          "completed": "done", "succeeded": "done", "cancelled": "done", "credit_insufficient": "prepare"}
ACTIONS = {
    "restore_archive": ("user", False),
    "retry": ("user", True),
    "confirm_stopped": ("operator", False),
    "inspect_original": ("operator", False),
    "reconcile_backend": ("worker", False),
    "finalize_output": ("worker", False),
}


def recovery_projection(kind, task_id, status, *, job_id=None, credit_status=None, stage=None,
                        failure_code=None, failure_message=None, evidence=None, actions=()):
    normalized = STATUSES.get(status, status)
    if normalized not in {"draft", "queued", "running", "completed", "failed", "uncertain", "cancelled", "blocked"}:
        normalized = "uncertain"
    failure = None
    if normalized in {"failed", "uncertain", "blocked"}:
        failure = {"code": failure_code or "execution_" + normalized,
                   "message": failure_message or "请核实原执行状态；不会自动重新生成。"}
    return {"schema_version": 1, "kind": kind, "task_id": task_id, "job_id": job_id,
            "status": normalized, "original_status": status, "stage": stage or STAGES.get(status, "unknown"),
            "failure": failure, "credit_status": credit_status, "automatic_retry": False,
            "evidence": {"execution_id": None, "has_candidate": False, "candidate_verified": False,
                         "has_completion_receipt": False, "has_backend_reference": False,
                         "has_output": False, **(evidence or {})},
            "actions": [{"code": code, "actor": ACTIONS[code][0], "may_invoke_model": ACTIONS[code][1],
                         "requires_confirmation": True} for code in actions]}
