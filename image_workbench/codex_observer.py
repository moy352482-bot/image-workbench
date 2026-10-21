"""Observe this execution's existing JSONL log without changing Codex's I/O.

CLI JSON events have no producer timestamps. Arrival times are host observations,
not image-model timings; post-exit draining cannot recover an event's start time.
"""

import json
from pathlib import Path
import threading


class CodexStdoutObserver:
    poll_interval = 0.05
    max_line_bytes = 1024 * 1024
    read_bytes = 256 * 1024

    def __init__(self, path: Path, performance) -> None:
        self.path = path
        self.performance = performance
        self._stop = threading.Event()
        self._thread = None
        self._offset = 0
        self._pending = b""
        self._discard_line = False

    def start(self) -> None:
        if self.performance is None:
            return
        try:
            # Runs normally begin with empty files; ignore any pre-existing bytes.
            self._offset = self.path.stat().st_size
            self._thread = threading.Thread(
                target=self._watch, name="codex-performance-observer", daemon=True,
            )
            self._thread.start()
        except Exception:
            # Optional instrumentation must never prevent image generation.
            self._thread = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread is None:
            return
        self._thread.join(timeout=1)
        if not self._thread.is_alive():
            try:
                while self._read("post_exit"):
                    pass
                if self._pending and not self._discard_line:
                    self._event(self._pending, "post_exit")
                self._pending = b""
            except Exception:
                pass

    def _watch(self) -> None:
        try:
            while not self._stop.is_set():
                self._read("live")
                self._stop.wait(self.poll_interval)
        except Exception:
            # A logging failure is observational only, never a backend failure.
            return

    def _read(self, phase: str) -> bool:
        with self.path.open("rb") as stream:
            stream.seek(self._offset)
            chunk = stream.read(self.read_bytes)
        if not chunk:
            return False
        self._offset += len(chunk)
        pieces = chunk.split(b"\n")
        for index, piece in enumerate(pieces):
            complete = index < len(pieces) - 1
            if not self._discard_line:
                if len(self._pending) + len(piece) <= self.max_line_bytes:
                    self._pending += piece
                else:
                    self._pending = b""
                    self._discard_line = True
            if complete:
                if not self._discard_line:
                    self._event(self._pending, phase)
                self._pending = b""
                self._discard_line = False
        return True

    def _event(self, line: bytes, phase: str) -> None:
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                return
            # Do not retain agent text, prompts, tool arguments, image data or errors.
            event = {"type": payload["type"][:200], "observation_phase": phase}
            for key in ("thread_id", "timestamp"):
                if isinstance(payload.get(key), str):
                    event[key] = payload[key][:200]
            item = payload.get("item")
            if isinstance(item, dict):
                event["item"] = {
                    key: item[key][:200]
                    for key in ("type", "id", "name", "tool", "server", "status")
                    if isinstance(item.get(key), str)
                }
            self.performance.observe_cli_event(event)
        except (ValueError, UnicodeError, TypeError):
            return
