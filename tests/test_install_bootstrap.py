from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "install.animemo.cc" / "install.sh"
GIT_SH = Path(r"C:\Program Files\Git\bin\sh.exe")


class InstallBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        if not GIT_SH.is_file():
            self.skipTest("Git for Windows POSIX shell is unavailable")

    @staticmethod
    def _posix(path: Path) -> str:
        resolved = path.resolve()
        return f"/{resolved.drive[0].lower()}{resolved.as_posix()[2:]}"

    def _run(self, *arguments: str, curl_body: str | None = None, chmod_fails: bool = False):
        workspace = tempfile.TemporaryDirectory()
        root = Path(workspace.name)
        tmp = root / "tmp"
        tmp.mkdir()
        harness = """
uname() { printf 'Linux\\n'; }
id() { printf '0\\n'; }
gh() { return 1; }
python3() { return 97; }
tar() { return 97; }
sha256sum() { return 97; }
curl() {
destination=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '--output' ]; then destination=$2; shift 2; else shift; fi
done
printf '%s' "$ANIMEMO_TEST_CURL_BODY" >"$destination"
}
"""
        if chmod_fails:
            harness += "chmod() { return 1; }\n"
        harness += 'candidate=$1\nshift\n. "$candidate" "$@"\n'
        environment = {
            "TMPDIR": self._posix(tmp),
            "ANIMEMO_TEST_CURL_BODY": curl_body or "",
        }
        result = subprocess.run(
            [str(GIT_SH), "-c", harness, "bootstrap-test", str(BOOTSTRAP), *arguments],
            env=environment,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return workspace, tmp, result

    def test_portable_and_unknown_sources_fail_before_any_transport(self):
        local_workspace, _, local = self._run("--source", "local-bundle")
        self.addCleanup(local_workspace.cleanup)
        self.assertEqual(local.returncode, 78)
        self.assertIn("BLOCKED_PORTABLE_PUBLICATION_AUTHORITY", local.stderr)

        unknown_workspace, _, unknown = self._run("--source", "auto")
        self.addCleanup(unknown_workspace.cleanup)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("TRANSPORT_SOURCE_UNSUPPORTED", unknown.stderr)

    def test_unsupported_argument_is_rejected_without_creating_staging(self):
        workspace, tmp, result = self._run("--repository", "attacker/repo")
        self.addCleanup(workspace.cleanup)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported argument", result.stderr)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_non_semver_release_is_rejected_before_transport(self):
        workspace, tmp, result = self._run(
            "--version",
            "v1.1.0-attacker",
            "--public-origin",
            "https://animemo.example",
        )
        self.addCleanup(workspace.cleanup)
        self.assertEqual(result.returncode, 2)
        self.assertIn("release version is invalid", result.stderr)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_github_download_failure_cleans_private_staging(self):
        workspace, tmp, result = self._run(
            "--source",
            "github",
            "--version",
            "v1.1.0",
            "--public-origin",
            "https://animemo.example",
        )
        self.addCleanup(workspace.cleanup)
        self.assertEqual(result.returncode, 69)
        self.assertIn("GitHub transport failed", result.stderr)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_mirror_empty_response_is_never_promoted_and_is_cleaned(self):
        workspace, tmp, result = self._run(
            "--source",
            "official-mirror",
            "--version",
            "v1.1.0",
            "--public-origin",
            "https://animemo.example",
            curl_body="",
        )
        self.addCleanup(workspace.cleanup)
        self.assertEqual(result.returncode, 69)
        self.assertIn("empty object", result.stderr)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_staging_permission_failure_is_fail_closed_and_cleaned(self):
        workspace, tmp, result = self._run(
            "--version",
            "v1.1.0",
            "--public-origin",
            "https://animemo.example",
            chmod_fails=True,
        )
        self.addCleanup(workspace.cleanup)
        self.assertEqual(result.returncode, 73)
        self.assertIn("temporary directory permission failed", result.stderr)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_complete_footer_is_required_before_main_can_execute(self):
        source = BOOTSTRAP.read_text(encoding="utf-8")
        truncated = source.rsplit('main "$@"', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "truncated.sh"
            candidate.write_text(truncated, encoding="utf-8", newline="\n")
            result = subprocess.run(
                [str(GIT_SH), str(candidate), "--source", "auto"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("TRANSPORT_SOURCE_UNSUPPORTED", result.stderr)

    def test_html_candidate_is_not_executed_as_a_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "html.sh"
            candidate.write_text("<!doctype html><title>not a script</title>\n", encoding="utf-8")
            result = subprocess.run(
                [str(GIT_SH), str(candidate)],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
