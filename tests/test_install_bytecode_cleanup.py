from __future__ import annotations

import importlib.util
import os
import py_compile
import shutil
import subprocess
import struct
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def actual_install_cleanup_command() -> str:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    commands = [line.strip() for line in installer.splitlines() if line.strip().startswith("find src/eisy_onewire ")]
    if len(commands) != 1:
        raise AssertionError("expected exactly one plugin-scoped cache cleanup command")
    return commands[0]


def load_value(source: Path) -> str:
    spec = importlib.util.spec_from_file_location("same_metadata_fixture", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("fixture module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VALUE


class InstallBytecodeCleanupTests(unittest.TestCase):
    def test_cleanup_prevents_same_timestamp_and_size_cache_collision(self) -> None:
        old_source = 'VALUE = "old"\n'
        new_source = 'VALUE = "new"\n'
        self.assertEqual(len(old_source.encode()), len(new_source.encode()))

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "src" / "eisy_onewire"
            package.mkdir(parents=True)
            source = package / "version.py"
            source.write_bytes(old_source.encode("utf-8"))
            os.utime(source, (0, 0))
            pyc = Path(
                py_compile.compile(
                    str(source),
                    doraise=True,
                    invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
                )
            )

            header = pyc.read_bytes()[:16]
            flags, cached_mtime, cached_size = struct.unpack("<III", header[4:16])
            self.assertEqual(flags, 0)
            self.assertEqual(cached_mtime, 0)
            self.assertEqual(cached_size, len(old_source.encode()))

            source.write_bytes(new_source.encode("utf-8"))
            os.utime(source, (0, 0))
            self.assertEqual(load_value(source), "old")

            shutil.rmtree(package / "__pycache__")

            self.assertEqual(load_value(source), "new")

    @unittest.skipUnless(os.name == "posix", "installer uses POSIX sh/find")
    def test_actual_installer_cleanup_is_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_cache = root / "src" / "eisy_onewire" / "__pycache__"
            nested_cache = root / "src" / "eisy_onewire" / "nested" / "__pycache__"
            unrelated_cache = root / ".local" / "lib" / "__pycache__"
            for cache in (package_cache, nested_cache, unrelated_cache):
                cache.mkdir(parents=True)
                (cache / "marker.pyc").write_bytes(b"keep")

            completed = subprocess.run(
                ["sh", "-c", actual_install_cleanup_command()],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(package_cache.exists())
            self.assertFalse(nested_cache.exists())
            self.assertEqual(
                (unrelated_cache / "marker.pyc").read_bytes(), b"keep"
            )



if __name__ == "__main__":
    unittest.main()
