"""Serialized, bounded command execution for runtime presentation changes."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .temperature_presentation import TemperatureUnit


PRESENTATION_QUEUE_CAPACITY = 16
PRESENTATION_SHUTDOWN_TIMEOUT_SECONDS = 25.0


class Logger(Protocol):
    def error(self, message: str, *args: object) -> None: ...


class PresentationWorkerShutdownTimeout(RuntimeError):
    """The presentation worker did not become quiescent before shutdown."""


class PresentationIntentKind(str, Enum):
    UNIT = "unit"
    PRECISION = "precision"


@dataclass(frozen=True, slots=True)
class PresentationIntent:
    """A validated command intent, deliberately not a preference snapshot."""

    kind: PresentationIntentKind
    value: TemperatureUnit | int

    @classmethod
    def unit(cls, value: TemperatureUnit) -> "PresentationIntent":
        return cls(PresentationIntentKind.UNIT, value)

    @classmethod
    def precision(cls, value: int) -> "PresentationIntent":
        return cls(PresentationIntentKind.PRECISION, value)


class PresentationCommandWorker:
    """Run presentation transactions serially outside udi's command thread."""

    def __init__(
        self,
        logger: Logger,
        processor: Callable[[PresentationIntent], None],
        *,
        queue_capacity: int = PRESENTATION_QUEUE_CAPACITY,
        shutdown_timeout_seconds: float = PRESENTATION_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("presentation queue capacity must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("presentation shutdown timeout must be positive")
        self._logger = logger
        self._processor = processor
        self._queue: queue.Queue[PresentationIntent] = queue.Queue(queue_capacity)
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._stopping = False

    def start(self) -> None:
        """Start the single worker without issuing a profile request."""

        with self._lock:
            if self._thread is not None:
                return
            self._stopping = False
            self._accepting = True
            self._thread = threading.Thread(
                target=self._run,
                name="eisy-onewire-presentation",
                daemon=True,
            )
            self._thread.start()

    def enqueue(self, intent: PresentationIntent) -> bool:
        """Accept one already-validated intent without blocking the caller."""

        with self._lock:
            if not self._accepting or self._stopping:
                return False
            try:
                self._queue.put_nowait(intent)
            except queue.Full:
                return False
        return True

    def stop(self) -> None:
        """Cancel queued work and wait for one bounded in-flight transaction."""

        with self._lock:
            self._accepting = False
            self._stopping = True
            thread = self._thread
            self._discard_queued_locked()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._shutdown_timeout_seconds)
            if thread.is_alive():
                detail = (
                    "presentation worker did not become quiescent within "
                    f"{self._shutdown_timeout_seconds:.3f} seconds; platform "
                    "interface shutdown is unsafe"
                )
                self._logger.error("%s", detail)
                raise PresentationWorkerShutdownTimeout(detail)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def wait_idle(self, timeout_seconds: float) -> bool:
        """Wait for deterministic tests and administrative health checks."""

        if timeout_seconds < 0:
            raise ValueError("presentation idle timeout cannot be negative")
        # Queue.join has no timeout. Polling unfinished_tasks is bounded and
        # never affects the worker's execution order.
        end = time.monotonic() + timeout_seconds
        while self._queue.unfinished_tasks:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, 0.01))
        return True

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
            try:
                intent = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    stopping = self._stopping
                if not stopping:
                    self._processor(intent)
            except Exception as exc:  # pragma: no cover - defensive containment
                self._logger.error(
                    "Unhandled presentation worker error: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            finally:
                self._queue.task_done()

    def _discard_queued_locked(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            else:
                self._queue.task_done()
