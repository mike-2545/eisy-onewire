"""One adapter lock shared by plugin threads and separate diagnostic processes."""

from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path
from typing import Protocol


class AdapterLockBackend(Protocol):
    def acquire(self, path: Path, *, blocking: bool) -> object | None: ...

    def release(self, token: object) -> None: ...


class AdapterAccessLock:
    """Combine the existing thread exclusion with a FreeBSD ``flock``."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        backend: AdapterLockBackend | None = None,
    ) -> None:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        self.path = path or Path(tempfile.gettempdir()) / (
            f"eisy-onewire-ds9490-{uid}.lock"
        )
        self._thread_lock = threading.Lock()
        self._backend = backend or _platform_backend()
        self._token: object | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if timeout != -1:
            acquired = self._thread_lock.acquire(blocking, timeout)
        else:
            acquired = self._thread_lock.acquire(blocking)
        if not acquired:
            return False
        try:
            token = self._backend.acquire(self.path, blocking=blocking)
        except Exception:
            self._thread_lock.release()
            raise
        if token is None:
            self._thread_lock.release()
            return False
        self._token = token
        return True

    def release(self) -> None:
        token = self._token
        if token is None:
            raise RuntimeError("adapter lock is not held")
        self._token = None
        try:
            self._backend.release(token)
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "AdapterAccessLock":
        self.acquire()
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        self.release()


class _FcntlBackend:
    def acquire(self, path: Path, *, blocking: bool) -> object | None:
        import fcntl

        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise RuntimeError("adapter lock path is not a regular file")
            if hasattr(os, "getuid") and details.st_uid != os.getuid():
                raise RuntimeError("adapter lock file is not owned by this plugin account")
            os.fchmod(descriptor, 0o600)
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(descriptor, operation)
            except OSError as exc:
                if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                    os.close(descriptor)
                    return None
                raise
            return descriptor
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    def release(self, token: object) -> None:
        import fcntl

        descriptor = int(token)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class _ProcessOnlyBackend:
    """Test/development fallback; production eisy runs FreeBSD with fcntl."""

    def acquire(self, path: Path, *, blocking: bool) -> object | None:
        del path, blocking
        return object()

    def release(self, token: object) -> None:
        del token


def _platform_backend() -> AdapterLockBackend:
    if sys.platform.startswith("freebsd"):
        return _FcntlBackend()
    return _ProcessOnlyBackend()
