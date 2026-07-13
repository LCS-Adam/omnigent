"""Async queue-based telemetry emitter.

Errors are silently swallowed — telemetry must never disrupt the
application.  All opt-out signals are checked in :func:`is_disabled`.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict
from typing import Any

_logger = logging.getLogger(__name__)

# CI / test environment variable names that indicate telemetry should be off.
_CI_ENV_VARS = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        "CIRCLECI",
        "JENKINS_URL",
        "TRAVIS",
        "GITLAB_CI",
        "TF_BUILD",
        "BITBUCKET_BUILD_NUMBER",
        "CODEBUILD_BUILD_ARN",
        "BUILDKITE",
        "TEAMCITY_VERSION",
    }
)

_BATCH_SIZE = 50
_BATCH_INTERVAL_S = 30.0
_MAX_QUEUE_SIZE = 512


def is_disabled() -> bool:
    """Return ``True`` when telemetry should be completely suppressed.

    Checks (in order):
    1. ``OMNIGENT_TELEMETRY=0``
    2. ``OMNIGENT_DISABLE_TELEMETRY=true`` (any truthy spelling)
    3. ``DO_NOT_TRACK=1``
    4. Any CI environment variable from :data:`_CI_ENV_VARS`

    Always returns a ``bool``; never raises.
    """
    try:
        if os.environ.get("OMNIGENT_TELEMETRY", "").strip() == "0":
            return True
        dnt_val = os.environ.get("OMNIGENT_DISABLE_TELEMETRY", "").strip().lower()
        if dnt_val in ("1", "true", "yes"):
            return True
        if os.environ.get("DO_NOT_TRACK", "").strip() == "1":
            return True
        for var in _CI_ENV_VARS:
            if var in os.environ:
                return True
        return False
    except Exception:
        return True


class TelemetryClient:
    """Fire-and-forget telemetry emitter backed by a background thread.

    Events are serialised to JSON and placed on an in-memory queue.
    A single daemon thread consumes the queue and POSTs batches to
    ``OMNIGENT_TELEMETRY_ENDPOINT`` when that variable is set.  If the
    variable is absent the queue is drained silently (useful for
    testing that instrumentation fires without a live endpoint).

    All errors in the background thread are suppressed.
    """

    def __init__(self) -> None:
        self._endpoint: str | None = os.environ.get("OMNIGENT_TELEMETRY_ENDPOINT")
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False
        self._atexit_registered = False
        self._thread: threading.Thread | None = None

    # ── Public interface ─────────────────────────────────

    def emit(self, event: object) -> None:
        """Queue an event for async delivery.

        Accepts any dataclass; converts it to ``{"event_type": ...,
        **fields}``.  Silently no-ops when disabled or stopped.

        :param event: A dataclass instance, e.g. :class:`SessionCreatedEvent`.
        """
        if self._stopped:
            return
        try:
            payload = {
                "event_type": type(event).__name__,
                **asdict(event),  # type: ignore[arg-type]
            }
            self._ensure_started()
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                pass  # drop silently
        except Exception:
            pass

    def flush(self) -> None:
        """Block until the queue is empty (used in tests and at shutdown)."""
        try:
            self._queue.join()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Signal the background thread to stop and wait briefly."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._queue.put_nowait(None)  # poison pill
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ── Internal helpers ─────────────────────────────────

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._thread = threading.Thread(
                target=self._consumer,
                name="OmnigentTelemetryConsumer",
                daemon=True,
            )
            self._thread.start()
            self._started = True
            if not self._atexit_registered:
                atexit.register(self._atexit_callback)
                self._atexit_registered = True

    def _atexit_callback(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass

    def _consumer(self) -> None:
        """Background thread: drain the queue in batches."""
        pending: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        while not self._stopped:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                if pending and time.monotonic() - last_flush >= _BATCH_INTERVAL_S:
                    self._send(pending)
                    pending = []
                    last_flush = time.monotonic()
                continue

            if item is None:
                # Poison pill — flush what we have, then exit.
                if pending:
                    self._send(pending)
                self._queue.task_done()
                break

            pending.append(item)
            self._queue.task_done()

            if len(pending) >= _BATCH_SIZE:
                self._send(pending)
                pending = []
                last_flush = time.monotonic()
            elif time.monotonic() - last_flush >= _BATCH_INTERVAL_S:
                self._send(pending)
                pending = []
                last_flush = time.monotonic()

        # Drain remaining on stop.
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    pending.append(item)
                self._queue.task_done()
            except queue.Empty:
                break
        if pending:
            self._send(pending)

    def _send(self, events: list[dict[str, Any]]) -> None:
        """POST a batch to the configured endpoint, or no-op if unset."""
        if not self._endpoint or not events:
            return
        try:
            import urllib.request

            body = json.dumps({"events": events}).encode("utf-8")
            req = urllib.request.Request(
                self._endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
        except Exception:
            pass


# ── Module-level singleton ───────────────────────────────

_CLIENT: TelemetryClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> TelemetryClient | None:
    """Return the active singleton client, or ``None`` when disabled."""
    return _CLIENT


def init_client() -> None:
    """Initialise the module-level client if telemetry is enabled.

    Safe to call multiple times; idempotent after the first call.
    """
    global _CLIENT
    if is_disabled():
        return
    with _CLIENT_LOCK:
        if _CLIENT is None:
            try:
                _CLIENT = TelemetryClient()
            except Exception:
                pass


def emit(event: object) -> None:
    """Emit an event through the module-level client.

    No-op when telemetry is disabled or the client is not initialised.
    Never raises.

    :param event: A dataclass instance.
    """
    try:
        if is_disabled():
            return
        client = _CLIENT
        if client is not None:
            client.emit(event)
    except Exception:
        pass
