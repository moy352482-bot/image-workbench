"""Optional, content-free performance observations; never drive generation decisions."""

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from threading import RLock
from time import perf_counter


class PerformanceTrace:
    def __init__(self):
        self._lock = RLock()
        self._origin = perf_counter()
        self.points = {}
        self.segments = []
        self.cli_events = []
        self.reference_count = 0
        self.mark("prepare_start")

    def _point(self):
        return {"at": datetime.now(timezone.utc).isoformat(),
                "offset_seconds": perf_counter() - self._origin}

    def mark(self, name, **metadata):
        with self._lock:
            self.points.setdefault(name, self._point())

    @contextmanager
    def span(self, name):
        start = self._point()
        try:
            yield
        finally:
            end = self._point()
            with self._lock:
                self.segments.append({"phase": name, "started_at": start["at"],
                                      "ended_at": end["at"],
                                      "seconds": end["offset_seconds"] - start["offset_seconds"]})

    def observe_cli_event(self, event):
        # Do not retain prompt, message, arguments, paths, tool output, or credentials.
        if not isinstance(event, dict):
            return
        with self._lock:
            if len(self.cli_events) >= 4096:
                return
            point = self._point()
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            row = {"event": str(event.get("type", ""))[:80],
                   "item_type": str(item.get("type", ""))[:80],
                   "item_id": str(item.get("id", ""))[:100],
                   "observed_at": point["at"], "offset_seconds": point["offset_seconds"],
                   "observation_phase": event.get("observation_phase", "live")}
            self.cli_events.append(row)
            if row["event"] == "turn.started" and row["observation_phase"] == "live":
                self.points.setdefault("codex_launch_end", point)
                self.points.setdefault("codex_execution_start", point)

    def _duration(self, start, end):
        a, b = self.points.get(start), self.points.get(end)
        if a is None or b is None or b["offset_seconds"] < a["offset_seconds"]:
            return None
        return b["offset_seconds"] - a["offset_seconds"]

    def build(self, task, record):
        self.mark("task_completed_time")
        completed = self.points["task_completed_time"]
        prepare_end = self.points.get("runner_prepare_end", completed)
        segments = [{"phase": "runner_prepare", "started_at": self.points["prepare_start"]["at"],
                     "ended_at": prepare_end["at"],
                     "seconds": prepare_end["offset_seconds"] - self.points["prepare_start"]["offset_seconds"]},
                    *self.segments,
                    *[dict(e) for e in record.timing_events if e["phase"] in {"validate", "register"}]]

        def seconds(*names):
            matching = [e["seconds"] for e in segments if e["phase"] in names and e.get("seconds") is not None]
            return sum(matching) if matching else None

        def bounds(*names):
            entries = [e for e in segments if e["phase"] in names]
            return (min(e["started_at"] for e in entries), max(e["ended_at"] for e in entries)) if entries else (None, None)

        combined = self._duration("codex_launch_start", "codex_execution_end")
        launch = self._duration("codex_launch_start", "codex_launch_end")
        execution = self._duration("codex_execution_start", "codex_execution_end")
        boundary_valid = combined is not None and launch is not None and execution is not None
        if not boundary_valid:
            launch = execution = None
        prepare = seconds("runner_prepare", "backend_prepare")
        collect, staging, validate = seconds("artifact_collect"), seconds("staging_copy"), seconds("validate")
        artifact_check = None if collect is None and validate is None else max(0.0, (collect or 0) - (staging or 0)) + (validate or 0)
        register = seconds("register", "staging_copy", "final_state_save")
        runner = self._duration("prepare_start", "task_completed_time")
        total = (datetime.fromisoformat(completed["at"]) - datetime.fromisoformat(task.created_at)).total_seconds()
        queue = (datetime.fromisoformat(self.points["prepare_start"]["at"]) - datetime.fromisoformat(task.created_at)).total_seconds()
        allocated = sum(v or 0 for v in (prepare, combined, artifact_check, register))
        timestamps = {"task_created_time": task.created_at, "task_completed_time": completed["at"]}
        timestamps["prepare_start"], timestamps["prepare_end"] = bounds("runner_prepare", "backend_prepare")
        for name in ("codex_launch_start", "codex_launch_end", "codex_execution_start", "codex_execution_end"):
            timestamps[name] = self.points.get(name, {}).get("at")
        for phase, names in (("artifact_check", ("artifact_collect", "validate")),
                             ("register", ("staging_copy", "register", "final_state_save"))):
            timestamps[phase + "_start"], timestamps[phase + "_end"] = bounds(*names)
        limitations = [
            "turn.started is a local CLI observation proxy, not proof that model inference has begun.",
            "CLI JSONL has no source timestamps; observed boundaries include log flush/poll delay (50 ms nominal polling).",
            "Codex execution includes scheduling, reasoning, ImageGen, response handling and process exit; it is not image-model compute time.",
            "No image-tool start/end events are exposed by the current CLI; ImageGen time is unknown.",
            "Repeated phase segments are summed; first/last timestamps are envelopes, not necessarily contiguous durations.",
            "Artifact collection includes staging copy; its measured copy is subtracted from check and assigned to register.",
            "total is task creation to runner completion (UTC); queue_or_idle includes time before invocation, not just queue scheduling.",
            "Final telemetry serialization is outside task completion; Worker settlement and HTTP delivery are outside this runner trace.",
        ]
        if not boundary_valid:
            limitations.append("No valid live turn.started boundary; codex_launch and codex_execution remain null. Use codex_combined.")
        if total < 0 or queue < 0 or abs(total - queue - runner) > 0.1:
            limitations.append("Wall-clock adjustment detected; prefer monotonic runner/phase durations over UTC total.")
        return {
            "schema_version": 1, "task_id": task.task_id, "project_id": task.project_id,
            "execution_id": record.execution_id, "operation": task.operation, "backend": record.backend,
            "prompt_type": "B_reference" if self.reference_count else "A_plain",
            "reference_count": self.reference_count, "output_count": len(record.output_artifact_ids),
            "status": "success" if record.status == "succeeded" else "failed",
            "execution_status": record.status, "failure_type": record.failure_type,
            "timestamps": timestamps,
            "durations_seconds": {"prepare": prepare, "codex_launch": launch, "codex_execution": execution,
                                  "codex_combined": combined, "artifact_check": artifact_check, "register": register,
                                  "total": total, "queue_or_idle": queue, "runner": runner,
                                  "other": max(0.0, runner - allocated)},
            "measurements": {"codex_boundary": "observed_live_turn_started" if boundary_valid else "unavailable",
                             "subprocess_spawn_seconds": self._duration("codex_launch_start", "subprocess_spawn_end"),
                             "imagegen_tool_seconds": None, "poll_interval_seconds": 0.05,
                             "clock": "perf_counter durations; timezone-aware UTC timestamps",
                             "limitations": limitations},
            "phase_segments": segments, "codex_events": list(self.cli_events),
        }

    def save(self, storage, task, record):
        """Telemetry failures must not change the task result or cause a retry."""
        try:
            payload = self.build(task, record)
            directory = storage.files.path("logs", "performance")
            directory.mkdir(parents=True, exist_ok=True)
            storage.files.write_json(storage.files.check(directory / f"{task.task_id}--{record.execution_id}.json"), payload)
        except Exception as exc:
            logging.getLogger(__name__).warning("Performance record was not saved (%s)", type(exc).__name__)
