from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from installer.offline_python_runtime import OfflineRuntimeError, install_wheel_runtime


class OfflinePythonRuntimeTests(unittest.TestCase):
    @staticmethod
    def _wheel(
        path: Path,
        members: dict[str, bytes],
        *,
        compression: int = zipfile.ZIP_STORED,
    ) -> None:
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, value in members.items():
                archive.writestr(name, value)

    def test_extracts_import_files_and_ignores_wheel_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            self._wheel(
                wheelhouse / "fixture-1-py3-none-any.whl",
                {
                    "fixture/__init__.py": b"VALUE = 7\n",
                    "fixture-1.data/purelib/extra.py": b"VALUE = 9\n",
                    "fixture-1.data/scripts/fixture": b"#!/bin/sh\n",
                },
            )
            target = root / "runtime"

            install_wheel_runtime(wheelhouse, target)

            self.assertEqual((target / "fixture/__init__.py").read_bytes(), b"VALUE = 7\n")
            self.assertEqual((target / "extra.py").read_bytes(), b"VALUE = 9\n")
            self.assertFalse((target / "fixture-1.data").exists())

    def test_rejects_parent_escape_and_removes_partial_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            self._wheel(
                wheelhouse / "fixture-1-py3-none-any.whl",
                {"fixture/__init__.py": b"", "../escape": b"forbidden"},
            )
            target = root / "runtime"

            with self.assertRaisesRegex(
                OfflineRuntimeError, "OFFLINE_PYTHON_RUNTIME_INVALID"
            ):
                install_wheel_runtime(wheelhouse, target)

            self.assertFalse(target.exists())
            self.assertFalse((root / "escape").exists())

    def test_rejects_cross_wheel_case_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            self._wheel(
                wheelhouse / "first-1-py3-none-any.whl",
                {"Package/module.py": b"first"},
            )
            self._wheel(
                wheelhouse / "second-1-py3-none-any.whl",
                {"package/MODULE.py": b"second"},
            )
            target = root / "runtime"

            with self.assertRaisesRegex(
                OfflineRuntimeError, "OFFLINE_PYTHON_RUNTIME_INVALID"
            ):
                install_wheel_runtime(wheelhouse, target)

            self.assertFalse(target.exists())

    def test_rejects_cross_wheel_nfc_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            self._wheel(
                wheelhouse / "first-1-py3-none-any.whl",
                {"package/caf\u00e9.py": b"first"},
            )
            self._wheel(
                wheelhouse / "second-1-py3-none-any.whl",
                {"package/cafe\u0301.py": b"second"},
            )
            target = root / "runtime"

            with self.assertRaisesRegex(
                OfflineRuntimeError, "OFFLINE_PYTHON_RUNTIME_INVALID"
            ):
                install_wheel_runtime(wheelhouse, target)

            self.assertFalse(target.exists())

    def test_rejects_high_compression_ratio_before_creating_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            self._wheel(
                wheelhouse / "fixture-1-py3-none-any.whl",
                {"fixture/payload.bin": b"A" * (1024 * 1024)},
                compression=zipfile.ZIP_DEFLATED,
            )
            target = root / "runtime"

            with self.assertRaisesRegex(
                OfflineRuntimeError, "OFFLINE_PYTHON_RUNTIME_INVALID"
            ):
                install_wheel_runtime(wheelhouse, target)

            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
