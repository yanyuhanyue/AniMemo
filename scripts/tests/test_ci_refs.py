import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPTS))

from ci_refs import _load_event, resolve_refs


class CIRefResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self._git("init")
        self._git("config", "user.email", "ci@example.test")
        self._git("config", "user.name", "CI")
        self.base = self._commit("base.txt", "base", "base")
        self.head = self._commit("head.txt", "head", "head")

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def _commit(self, name, content, message):
        (self.repo / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def test_pull_request_uses_base_sha(self):
        refs = resolve_refs(
            repo=self.repo,
            env={"GITHUB_EVENT_NAME": "pull_request", "GITHUB_SHA": self.head},
            event={"pull_request": {"base": {"sha": self.base}}},
        )
        self.assertEqual((refs.base, refs.head), (self.base, self.head))
        self.assertIn("pull_request", refs.source)

    def test_push_uses_before_sha(self):
        refs = resolve_refs(
            repo=self.repo,
            env={"GITHUB_EVENT_NAME": "push", "GITHUB_SHA": self.head},
            event={"before": self.base},
        )
        self.assertEqual(refs.base, self.base)
        self.assertEqual(refs.source, "github.event.before")

    def test_push_all_zero_before_falls_back_to_parent(self):
        refs = resolve_refs(
            repo=self.repo,
            env={"GITHUB_EVENT_NAME": "push", "GITHUB_SHA": self.head},
            event={"before": "0" * 40},
        )
        self.assertEqual(refs.base, self.base)
        self.assertIn("fallback", refs.source)

    def test_workflow_dispatch_uses_explicit_input(self):
        refs = resolve_refs(
            repo=self.repo,
            env={"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": self.head},
            event={"inputs": {"upgrade_base_sha": self.base}},
        )
        self.assertEqual(refs.base, self.base)
        self.assertIn("upgrade_base_sha", refs.source)

    def test_workflow_dispatch_without_input_logs_parent_fallback(self):
        refs = resolve_refs(
            repo=self.repo,
            env={"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": self.head},
            event={"inputs": {}},
        )
        self.assertEqual(refs.base, self.base)
        self.assertIn("fallback HEAD^", refs.source)

    def test_workflow_call_uses_release_upgrade_base(self):
        refs = resolve_refs(
            repo=self.repo,
            env={"GITHUB_EVENT_NAME": "workflow_call", "GITHUB_SHA": self.head},
            event={"inputs": {"upgrade_base_sha": self.base}},
        )
        self.assertEqual((refs.base, refs.head), (self.base, self.head))
        self.assertEqual(refs.source, "workflow_call upgrade_base_sha")

    def test_runner_event_path_loads_the_runner_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(
                json.dumps({"before": self.base}),
                encoding="utf-8",
            )

            event = _load_event({"GITHUB_EVENT_PATH": str(event_path)})

        self.assertEqual(event, {"before": self.base})
