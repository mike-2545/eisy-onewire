"""Bounded, opt-in recovery of an already accepted DS9490 identity."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .usb_detector import UsbDeviceDescriptor


ADAPTER_RECOVERY_CONFIGURATION_KEY = "adapter_identity_recovery"
ADAPTER_RECOVERY_NOTICE_KEY = "adapter_identity_recovery"
ADAPTER_RECOVERY_SCHEMA_VERSION = 1
READINESS_WINDOW_SECONDS = 8.0
READINESS_POLL_SECONDS = 0.25
SETTLEMENT_SECONDS = 0.5
SHUTDOWN_TIMEOUT_SECONDS = 12.0


class AdapterRecoveryConfigurationError(ValueError):
    """Persisted automatic recovery authorization is malformed."""


class AdapterRecoveryShutdownTimeout(RuntimeError):
    """The recovery worker did not quiesce before interface shutdown."""


@dataclass(frozen=True, slots=True)
class AdapterRecoveryPolicy:
    """Explicit operator authorization; disabled is the safe default."""

    enabled: bool = False

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": ADAPTER_RECOVERY_SCHEMA_VERSION,
            "enabled": self.enabled,
        }

    @classmethod
    def from_data(cls, raw: object) -> "AdapterRecoveryPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise AdapterRecoveryConfigurationError(
                "adapter recovery configuration must be an object"
            )
        if set(raw) != {"schema_version", "enabled"}:
            raise AdapterRecoveryConfigurationError(
                "adapter recovery configuration fields are invalid"
            )
        if raw.get("schema_version") != ADAPTER_RECOVERY_SCHEMA_VERSION:
            raise AdapterRecoveryConfigurationError(
                f"adapter recovery schema_version must be {ADAPTER_RECOVERY_SCHEMA_VERSION}"
            )
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            raise AdapterRecoveryConfigurationError(
                "adapter recovery enabled state must be Boolean"
            )
        return cls(enabled)


class Logger(Protocol):
    def info(self, message: str, *args: object) -> None: ...

    def warning(self, message: str, *args: object) -> None: ...

    def error(self, message: str, *args: object) -> None: ...


class AdapterRecoveryWorker:
    """Serialize one bounded verification attempt per distinct recovery event.

    Descriptor enumeration is readiness observation only.  It never proves
    identity and never opens a device.  The injected ``verify`` callback owns
    the shared adapter lock and performs the single authorized identity Search
    ROM through the handle it retains on success.
    """

    def __init__(
        self,
        logger: Logger,
        *,
        authorized: Callable[[], bool],
        has_accepted_identity: Callable[[], bool],
        already_verified: Callable[[], bool],
        enumerate_candidates: Callable[[], tuple[UsbDeviceDescriptor, ...]],
        verify: Callable[[str], bool],
        publish: Callable[[dict[str, object]], None],
        readiness_window_seconds: float = READINESS_WINDOW_SECONDS,
        readiness_poll_seconds: float = READINESS_POLL_SECONDS,
        settlement_seconds: float = SETTLEMENT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        shutdown_timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if readiness_window_seconds < 0:
            raise ValueError("readiness_window_seconds cannot be negative")
        if readiness_poll_seconds <= 0 or settlement_seconds < 0:
            raise ValueError("recovery readiness timing is invalid")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._logger = logger
        self._authorized = authorized
        self._has_accepted_identity = has_accepted_identity
        self._already_verified = already_verified
        self._enumerate_candidates = enumerate_candidates
        self._verify = verify
        self._publish_callback = publish
        self._readiness_window_seconds = readiness_window_seconds
        self._readiness_poll_seconds = readiness_poll_seconds
        self._settlement_seconds = settlement_seconds
        self._monotonic = monotonic
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._pending_reason: str | None = None
        self._running = False
        self._stopping = False

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                name="eisy-onewire-adapter-recovery",
                daemon=True,
            )
            self._thread.start()

    def trigger(self, reason: str) -> bool:
        """Queue one event; duplicates coalesce without another transaction."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("recovery reason must be non-empty text")
        with self._condition:
            if self._stopping or self._thread is None:
                return False
            if self._pending_reason is not None or self._running:
                return False
            self._pending_reason = reason.strip()
            self._condition.notify_all()
            return True

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._pending_reason = None
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._shutdown_timeout_seconds)
            if thread.is_alive():
                raise AdapterRecoveryShutdownTimeout(
                    "adapter recovery worker did not become quiescent before "
                    "platform interface shutdown"
                )
        with self._condition:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def wait_idle(self, timeout_seconds: float = 2.0) -> bool:
        deadline = self._monotonic() + timeout_seconds
        with self._condition:
            while self._pending_reason is not None or self._running:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending_reason is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                reason = self._pending_reason
                self._pending_reason = None
                self._running = True
            assert reason is not None
            try:
                self._process(reason)
            except Exception as exc:  # fail closed at the worker boundary
                self._logger.error(
                    "Automatic adapter identity recovery failed unexpectedly: %s", exc
                )
                self._publish(
                    {
                        "status": "transport_failure",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "event": reason,
                        "identity_transactions": 0,
                        "automatic_retries": 0,
                    }
                )
            finally:
                with self._condition:
                    self._running = False
                    self._condition.notify_all()

    def _process(self, reason: str) -> None:
        if not self._authorized():
            self._publish_status("disabled", reason, identity_transactions=0)
            return
        if not self._has_accepted_identity():
            self._publish_status("no_accepted_identity", reason, identity_transactions=0)
            return
        if self._already_verified():
            self._publish_status("already_verified", reason, identity_transactions=0)
            return

        deadline = self._monotonic() + self._readiness_window_seconds
        self._publish_status("waiting_for_adapter", reason, identity_transactions=0)
        while True:
            if self._cancelled():
                return
            candidates = self._enumerate_candidates()
            if len(candidates) > 1:
                self._publish_status(
                    "multiple_adapters", reason, identity_transactions=0,
                    matching_adapter_count=len(candidates),
                )
                return
            if len(candidates) == 1:
                if self._wait_cancelable(self._settlement_seconds):
                    return
                if not self._authorized():
                    self._publish_status(
                        "disabled", reason, identity_transactions=0
                    )
                    return
                # Re-observe once after permission/device-node settlement.  A
                # changed candidate set blocks the transaction rather than
                # selecting by stale location evidence.
                settled = self._enumerate_candidates()
                if len(settled) != 1:
                    status = "multiple_adapters" if len(settled) > 1 else "adapter_absent"
                    self._publish_status(
                        status,
                        reason,
                        identity_transactions=0,
                        matching_adapter_count=len(settled),
                    )
                    return
                if self._already_verified():
                    self._publish_status(
                        "already_verified", reason, identity_transactions=0
                    )
                    return
                if not self._authorized():
                    self._publish_status(
                        "disabled", reason, identity_transactions=0
                    )
                    return
                succeeded = bool(self._verify(reason))
                self._publish_status(
                    "verified" if succeeded else "verification_failed",
                    reason,
                    identity_transactions=1,
                )
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                self._publish_status(
                    "adapter_absent", reason, identity_transactions=0,
                    matching_adapter_count=0,
                )
                return
            if self._wait_cancelable(min(self._readiness_poll_seconds, remaining)):
                return

    def _publish_status(
        self,
        status: str,
        event: str,
        *,
        identity_transactions: int,
        matching_adapter_count: int | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "status": status,
            "event": event,
            "authorized": self._authorized(),
            "identity_transactions": identity_transactions,
            "automatic_retries": 0,
            "identity_replacement_allowed": False,
            "topology_discovery": False,
            "branch_switching": False,
            "eeprom_writes": 0,
        }
        if matching_adapter_count is not None:
            payload["matching_adapter_count"] = matching_adapter_count
        self._publish(payload)

    def _publish(self, payload: dict[str, object]) -> None:
        with self._condition:
            if self._stopping:
                return
        self._publish_callback(payload)

    def _wait_cancelable(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._cancelled()
        with self._condition:
            self._condition.wait(seconds)
            return self._stopping

    def _cancelled(self) -> bool:
        with self._condition:
            return self._stopping
