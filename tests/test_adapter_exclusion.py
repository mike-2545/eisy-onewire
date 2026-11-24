from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from eisy_onewire.adapter_exclusion import AdapterAccessLock, _FcntlBackend


class SharedBackend:
    def __init__(self) -> None:
        self.held = False
        self.releases = 0

    def acquire(self, path: Path, *, blocking: bool):
        del path, blocking
        if self.held:
            return None
        self.held = True
        return object()

    def release(self, token: object) -> None:
        del token
        self.held = False
        self.releases += 1


class AdapterExclusionTests(unittest.TestCase):
    def test_two_process_facades_cannot_hold_the_same_backend_lock(self) -> None:
        backend = SharedBackend()
        first = AdapterAccessLock(Path("shared"), backend=backend)
        second = AdapterAccessLock(Path("shared"), backend=backend)

        self.assertTrue(first.acquire(blocking=False))
        self.assertFalse(second.acquire(blocking=False))
        first.release()
        self.assertTrue(second.acquire(blocking=False))
        second.release()
        self.assertEqual(backend.releases, 2)

    @unittest.skipIf(sys.platform == "win32", "fcntl is unavailable on Windows")
    def test_fcntl_backend_enforces_real_advisory_exclusion_and_mode(self) -> None:
        backend = _FcntlBackend()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adapter.lock"
            first = backend.acquire(path, blocking=False)
            self.assertIsNotNone(first)
            second = backend.acquire(path, blocking=False)
            self.assertIsNone(second)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            backend.release(first)
            third = backend.acquire(path, blocking=False)
            self.assertIsNotNone(third)
            backend.release(third)


if __name__ == "__main__":
    unittest.main()
