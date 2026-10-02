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
    def test_online_entry_requires_independent_trust_before_execution(self):
        source = (ROOT / "docs" / "distribution-transports-v1.1.md").read_text(encoding="utf-8")
        entry = source.split("# OFFICIAL_MIRROR_STAGE0_BEGIN", 1)[1].split("# OFFICIAL_MIRROR_STAGE0_END", 1)[0]
        self.assertIn("BOOTSTRAP_TOKENLESS_INDEPENDENT_TRUST_REQUIRED", entry)
        for command in ("sudo", "gh auth", "tar ", "python3", "curl"):
            self.assertNotIn(command, entry)

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
