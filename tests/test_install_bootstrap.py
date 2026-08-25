from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "sites" / "install-portal" / "install.sh"
TRANSPORT_DOC = ROOT / "docs" / "distribution-transports-v1.1.md"
GIT_SH = Path(r"C:\Program Files\Git\bin\sh.exe")


class InstallBootstrapRetirementTests(unittest.TestCase):
    def test_root_owned_copy_is_independently_reverified_before_extraction(self) -> None:
        source = TRANSPORT_DOC.read_text(encoding="utf-8")
        copied = source.index(
            '/usr/bin/install -o root -g root -m 0600 "$MIRROR_CANDIDATE" "$HANDOFF"'
        )
        protected_verification = source.index(
            '/usr/bin/gh release verify-asset "$EXACT_TAG" "$PROTECTED"'
        )
        extracted = source.index('/usr/bin/tar -xf "$PROTECTED"')
        runtime_created = source.index('/usr/bin/python3 -P -B -m venv "$RUNTIME"')
        dependencies_installed = source.index(
            '"$RUNTIME/bin/python" -P -B -m pip install'
        )
        python_started = source.index(
            '"$RUNTIME/bin/python" -P -B -m installer'
        )

        self.assertLess(copied, protected_verification)
        self.assertLess(protected_verification, extracted)
        self.assertLess(extracted, runtime_created)
        self.assertLess(runtime_created, dependencies_installed)
        self.assertLess(dependencies_installed, python_started)

    def test_protected_runtime_uses_only_archive_bound_binary_wheels(self) -> None:
        source = TRANSPORT_DOC.read_text(encoding="utf-8")

        for required in (
            "--no-cache-dir",
            "--no-index",
            "--only-binary=:all:",
            '--find-links "$MATERIALS/wheelhouse"',
            '"$MATERIALS/release/requirements.txt"',
            '"$MATERIALS/durability/requirements.txt"',
        ):
            self.assertIn(required, source)
        self.assertNotIn(
            "PYTHONSAFEPATH=1 /usr/bin/python3 -P -B -m installer", source
        )

    def test_safe_path_prevents_cwd_installer_shadow_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attacker = root / "attacker"
            protected = root / "protected"
            for location, marker in ((attacker, "ATTACKER"), (protected, "PROTECTED")):
                package = location / "installer"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("", encoding="utf-8")
                (package / "__main__.py").write_text(
                    f"print({marker!r})\n", encoding="utf-8"
                )
            result = subprocess.run(
                [sys.executable, "-P", "-B", "-m", "installer"],
                cwd=attacker,
                env={"PYTHONPATH": str(protected), "PYTHONSAFEPATH": "1"},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "PROTECTED")

    def test_remote_script_is_a_fail_closed_tombstone(self) -> None:
        source = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("REMOTE_BOOTSTRAP_EXECUTION_DISABLED", source)
        for forbidden in (
            "sudo",
            "apt-get",
            "systemctl",
            "docker",
            "curl",
            "gh release",
            "python3",
            "tar ",
            "eval ",
        ):
            self.assertNotIn(forbidden, source)

    def test_remote_script_cannot_download_mutate_or_execute(self) -> None:
        shell = GIT_SH if os.name == "nt" else Path("/bin/sh")
        if not shell.is_file():
            self.skipTest("POSIX shell is unavailable")
        result = subprocess.run(
            [str(shell), str(BOOTSTRAP)],
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("REMOTE_BOOTSTRAP_EXECUTION_DISABLED", result.stderr)


if __name__ == "__main__":
    unittest.main()
