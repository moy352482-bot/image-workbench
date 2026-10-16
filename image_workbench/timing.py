"""UTC 时间用于定位日志，perf_counter 用于耗时（不受系统校时影响）。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from time import perf_counter


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def measure(events, phase):
    event = {"phase": phase, "started_at": utc_now(), "ended_at": None, "seconds": None}
    events.append(event)
    started = perf_counter()
    try:
        yield event
    finally:
        event["seconds"] = perf_counter() - started
        event["ended_at"] = utc_now()


def phase_seconds(events, phase, default=0.0):
    values = [event["seconds"] for event in events if event["phase"] == phase and event["seconds"] is not None]
    return sum(values) if values else default
