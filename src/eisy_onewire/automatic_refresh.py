"""One serialized, monotonic scheduler for authorized DS18B20 nodes."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from .ds2409_transport import BranchHop, branch_path_label


AUTOMATIC_REFRESH_NOTICE_KEY = "automatic_refresh_status"
INITIAL_DELAY_SECONDS = 2.0
MINIMUM_START_STAGGER_SECONDS = 2.0
MINIMUM_OPERATION_GAP_SECONDS = 2.0
MAXIMUM_IDLE_WAIT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


class SchedulerShutdownTimeout(RuntimeError):
    """The worker did not reach quiescence before the shutdown deadline."""


class Logger(Protocol):
    def info(self, message: str, *args: object) -> None: ...

    def warning(self, message: str, *args: object) -> None: ...

    def error(self, message: str, *args: object) -> None: ...


@dataclass(frozen=True, slots=True)
class RefreshTarget:
    address: str
    rom: bytes
    name: str
    interval_seconds: int
    branch_path: tuple[BranchHop, ...]
    refresh: Callable[[], bool]
    last_error: Callable[[], str | None]
    automatic_eligible: bool = True


@dataclass(slots=True)
class _TargetState:
    target: RefreshTarget
    next_due: float | None = None
    last_attempt: float | None = None
    last_success: float | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    error: str | None = None
    in_progress: bool = False


class AutomaticRefreshScheduler:
    """Run at most one authorized refresh at a time without retry bursts."""

    def __init__(
        self,
        logger: Logger,
        notices: object,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        shutdown_timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._logger = logger
        self._notices = notices
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._master_enabled = False
        self._armed = False
        self._adapter_accessible = False
        self._configuration_valid = False
        self._states: dict[str, _TargetState] = {}
        self._next_operation_not_before = 0.0
        self._operation_in_progress = False

    def start(self) -> None:
        """Start one daemon worker; starting never performs a refresh by itself."""

        with self._lock:
            if self._thread is not None:
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                name="eisy-onewire-auto-refresh",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Cancel future work and wait for any bounded in-flight read."""

        with self._lock:
            self._stopping = True
            thread = self._thread
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._shutdown_timeout_seconds)
            if thread.is_alive():
                detail = (
                    "automatic refresh worker did not become quiescent within "
                    f"{self._shutdown_timeout_seconds:.3f} seconds; platform "
                    "interface shutdown is unsafe"
                )
                self._logger.error("%s", detail)
                raise SchedulerShutdownTimeout(detail)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def configure(
        self,
        *,
        master_enabled: bool,
        armed: bool,
        adapter_accessible: bool,
        configuration_valid: bool,
        targets: tuple[RefreshTarget, ...],
    ) -> None:
        """Replace desired state without performing USB work."""

        now = self._monotonic()
        with self._lock:
            was_active = self._active_locked()
            self._master_enabled = bool(master_enabled)
            self._armed = bool(armed)
            self._adapter_accessible = bool(adapter_accessible)
            self._configuration_valid = bool(configuration_valid)
            active = self._active_locked()
            previous = self._states
            states: dict[str, _TargetState] = {}
            stagger_index = 0
            for target in sorted(targets, key=lambda item: item.address):
                prior = previous.get(target.address)
                state = _TargetState(target)
                if prior is not None and prior.target.rom == target.rom:
                    state.last_attempt = prior.last_attempt
                    state.last_success = prior.last_success
                    state.last_attempt_at = prior.last_attempt_at
                    state.last_success_at = prior.last_success_at
                    state.error = prior.error
                    state.in_progress = prior.in_progress
                if not target.automatic_eligible:
                    state.error = (
                        "unattended DS2409 branch access is disabled; manual refresh only"
                    )
                elif target.interval_seconds == 0:
                    state.error = None
                elif active:
                    unchanged_active = (
                        was_active
                        and prior is not None
                        and prior.target.interval_seconds == target.interval_seconds
                        and prior.target.automatic_eligible
                        and prior.next_due is not None
                    )
                    if unchanged_active:
                        state.next_due = prior.next_due
                    else:
                        state.next_due = (
                            now
                            + INITIAL_DELAY_SECONDS
                            + stagger_index * MINIMUM_START_STAGGER_SECONDS
                        )
                    stagger_index += 1
                states[target.address] = state
            self._states = states
            if not active:
                self._next_operation_not_before = 0.0
            self._publish_locked(now)
        self._wake.set()

    def run_due_once(self) -> bool:
        """Run one due target; exposed for deterministic mocked tests."""

        now = self._monotonic()
        with self._lock:
            if (
                self._stopping
                or self._operation_in_progress
                or not self._active_locked()
            ):
                return False
            due = [
                state
                for state in self._states.values()
                if state.next_due is not None
                and not state.in_progress
                and state.next_due <= now
            ]
            if not due or now < self._next_operation_not_before:
                return False
            state = min(due, key=lambda item: (item.next_due or now, item.target.address))
            state.in_progress = True
            self._operation_in_progress = True
            state.next_due = None
            state.last_attempt = now
            state.last_attempt_at = _timestamp(self._utcnow())
            state.error = None
            target = state.target
            self._publish_locked(now)

        succeeded = False
        error: str | None = None
        try:
            succeeded = bool(target.refresh())
            if not succeeded:
                error = target.last_error() or "refresh failed or adapter was busy"
        except Exception as exc:  # defense at the scheduler boundary
            error = f"{type(exc).__name__}: {exc}"
            self._logger.error(
                "Scheduled refresh for %s raised unexpectedly: %s", target.name, error
            )

        completed = self._monotonic()
        with self._lock:
            self._operation_in_progress = False
            current = self._states.get(target.address)
            if current is not None and current.target.rom == target.rom:
                current.in_progress = False
                if succeeded:
                    current.last_success = completed
                    current.last_success_at = _timestamp(self._utcnow())
                    current.error = None
                else:
                    current.error = error or "refresh failed"
                if (
                    self._active_locked()
                    and current.target.automatic_eligible
                    and current.target.interval_seconds > 0
                ):
                    current.next_due = completed + current.target.interval_seconds
            self._next_operation_not_before = (
                completed + MINIMUM_OPERATION_GAP_SECONDS
            )
            self._publish_locked(completed)
        self._wake.set()
        return True

    def snapshot(self) -> dict[str, object]:
        """Return the same supported notice payload used by the plugin."""

        now = self._monotonic()
        with self._lock:
            return self._payload_locked(now)

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
                delay = self._next_wait_locked(self._monotonic())
            self._wake.wait(delay)
            self._wake.clear()
            with self._lock:
                if self._stopping:
                    return
            self.run_due_once()

    def _next_wait_locked(self, now: float) -> float:
        if not self._active_locked():
            return MAXIMUM_IDLE_WAIT_SECONDS
        due = [
            state.next_due
            for state in self._states.values()
            if state.next_due is not None and not state.in_progress
        ]
        if not due:
            return MAXIMUM_IDLE_WAIT_SECONDS
        next_due = max(min(due), self._next_operation_not_before)
        return min(MAXIMUM_IDLE_WAIT_SECONDS, max(0.0, next_due - now))

    def _active_locked(self) -> bool:
        return (
            self._master_enabled
            and self._armed
            and self._adapter_accessible
            and self._configuration_valid
        )

    def _publish_locked(self, now: float) -> None:
        self._notices[AUTOMATIC_REFRESH_NOTICE_KEY] = json.dumps(
            self._payload_locked(now), sort_keys=True
        )

    def _payload_locked(self, now: float) -> dict[str, object]:
        if not self._configuration_valid:
            status = "invalid_configuration"
        elif not self._adapter_accessible:
            status = "adapter_unavailable"
        elif not self._armed:
            status = "disarmed"
        elif not self._master_enabled:
            status = "disabled"
        else:
            status = "active"
        sensors = []
        for state in sorted(self._states.values(), key=lambda item: item.target.address):
            target = state.target
            if not target.automatic_eligible:
                eligibility = "branch_policy_disabled"
            elif target.interval_seconds == 0:
                eligibility = "off"
            elif target.branch_path:
                eligibility = "scheduled_branch"
            else:
                eligibility = "scheduled_root"
            sensors.append(
                {
                    "address": target.address,
                    "rom": target.rom.hex().upper(),
                    "name": target.name,
                    "interval_seconds": target.interval_seconds,
                    "eligibility": eligibility,
                    "branch_path": (
                        branch_path_label(target.branch_path)
                        if target.branch_path
                        else None
                    ),
                    "in_progress": state.in_progress,
                    "last_attempt_monotonic": state.last_attempt,
                    "last_success_monotonic": state.last_success,
                    "last_attempt_at": state.last_attempt_at,
                    "last_success_at": state.last_success_at,
                    "next_due_monotonic": state.next_due,
                    "next_due_in_seconds": (
                        max(0.0, round(state.next_due - now, 3))
                        if state.next_due is not None
                        else None
                    ),
                    "last_error": state.error,
                }
            )
        return {
            "status": status,
            "master_enabled": self._master_enabled,
            "scheduler_armed": self._armed,
            "adapter_accessible": self._adapter_accessible,
            "configuration_valid": self._configuration_valid,
            "timing_clock": "monotonic",
            "automatic_search_rom": False,
            "automatic_ds2409_switching": (
                self._active_locked()
                and any(
                    state.target.branch_path
                    and state.target.automatic_eligible
                    and state.target.interval_seconds > 0
                    for state in self._states.values()
                )
            ),
            "automatic_retries": 0,
            "minimum_operation_gap_seconds": MINIMUM_OPERATION_GAP_SECONDS,
            "operation_in_progress": self._operation_in_progress,
            "sensors": sensors,
        }


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
