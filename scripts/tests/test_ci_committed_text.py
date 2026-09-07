from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import check_committed_text as checker


class CommittedTextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="animemo-committed-text-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repository"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Isolated regression fixture")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.base = self.commit("README.md", "clean base\n")

    def git(self, *arguments, repo=None, check=True):
        return subprocess.run(
            ["git", *arguments], cwd=repo or self.root, check=check,
            capture_output=True, text=True, encoding="utf-8",
        )

    def commit(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        self.git("add", "--", name)
        self.git("commit", "-qm", "isolated fixture")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def check(self, event="pull_request", *, base=None, head=None, repo=None, fetch=False):
        return checker.check_committed_text(
            repo=repo or self.root, event_name=event,
            base=self.base if base is None else base,
            head=head or self.git("rev-parse", "HEAD", repo=repo).stdout.strip(),
            fetch_missing=fetch,
        )

    def test_clean_commits_pass_all_events_and_documentation_paths(self):
        head = self.commit("docs/guide.md", "documented behavior\n")
        for event in sorted(checker.EVENTS):
            with self.subTest(event=event):
                result = self.check(event, head=head)
                self.assertEqual(result["status"], "PASS")
                self.assertEqual(result["diff_base"], self.base)

    def test_actual_bad_commit_fails_while_old_worktree_check_passes(self):
        head = self.commit("docs/guide.md", "committed trailing whitespace   \n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.git("diff", "--check", check=False).returncode, 0)
        for event in sorted(checker.EVENTS):
            with self.subTest(event=event), self.assertRaisesRegex(checker.CommittedTextError, "trailing whitespace"):
                self.check(event, head=head)

    def test_cli_rejects_committed_defect_without_a_shell(self):
        head = self.commit("docs/cli.md", "bad whitespace   \n")
        helper = Path(checker.__file__).resolve()
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(helper), "--event-name", "pull_request", "--base", self.base, "--head", head],
            cwd=self.root, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Committed text failed", result.stderr)

    def test_initial_push_checks_root_and_all_introduced_commits(self):
        root = self.base
        self.assertEqual(self.check("push", base=checker.ZERO_SHA, head=root)["scope"], "initial-push-entire-tree")
        self.commit("docs/earlier.md", "bad in earlier commit   \n")
        self.commit("docs/latest.md", "clean latest commit\n")
        with self.assertRaisesRegex(checker.CommittedTextError, "trailing whitespace"):
            self.check("push", base=checker.ZERO_SHA)
        # An entirely new repository's root commit must be checked too.
        self.git("checkout", "--orphan", "new-root", "-q")
        self.git("rm", "-rf", ".")
        self.commit("root.md", "root has trailing whitespace   \n")
        with self.assertRaisesRegex(checker.CommittedTextError, "trailing whitespace"):
            self.check("push", base=checker.ZERO_SHA)

    def test_zero_or_missing_base_is_rejected_outside_initial_push(self):
        for event, base in (("pull_request", checker.ZERO_SHA), ("workflow_call", ""), ("push", ""), ("merge_group", "")):
            with self.subTest(event=event, base=base), self.assertRaises(checker.CommittedTextError):
                self.check(event, base=base)

    def test_pr_scope_uses_merge_base_without_checking_unrelated_base_branch_lines(self):
        self.git("checkout", "-qb", "candidate")
        candidate = self.commit("docs/candidate.md", "candidate is clean\n")
        self.git("checkout", "-q", "main")
        unrelated_base = self.commit("docs/main-only.md", "unrelated base whitespace   \n")
        self.git("checkout", "-q", "candidate")
        for event in ("pull_request", "merge_group", "workflow_call", "workflow_dispatch"):
            with self.subTest(event=event):
                result = self.check(event, base=unrelated_base, head=candidate)
                self.assertEqual(result["diff_base"], self.base)
        self.commit("docs/candidate.md", "candidate now has whitespace   \n")
        with self.assertRaisesRegex(checker.CommittedTextError, "trailing whitespace"):
            self.check(base=unrelated_base)

    def test_push_checks_the_entire_before_to_head_span(self):
        self.commit("docs/first.md", "defect in first pushed commit   \n")
        self.commit("docs/last.md", "clean final pushed commit\n")
        with self.assertRaisesRegex(checker.CommittedTextError, "trailing whitespace"):
            self.check("push")

    def test_rename_does_not_hide_new_whitespace(self):
        self.git("mv", "README.md", "renamed.md")
        self.commit("renamed.md", "clean base\nadded whitespace   \n")
        with self.assertRaisesRegex(checker.CommittedTextError, "renamed.md"):
            self.check()

    def test_reversed_commits_or_unchecked_head_are_rejected(self):
        later = self.commit("docs/later.md", "clean\n")
        with self.assertRaisesRegex(checker.CommittedTextError, "checked-out"):
            self.check(head=self.base)
        self.git("checkout", "--detach", "-q", self.base)
        with self.assertRaisesRegex(checker.CommittedTextError, "wrong diff direction"):
            self.check(base=later, head=self.base)

    def test_missing_objects_and_unrelated_history_never_become_empty_diff(self):
        with self.assertRaisesRegex(checker.CommittedTextError, "Required commit object"):
            self.check(base="f" * 40)
        self.git("checkout", "--orphan", "unrelated", "-q")
        self.git("rm", "-rf", ".")
        self.commit("unrelated.md", "clean\n")
        with self.assertRaisesRegex(checker.CommittedTextError, "merge-base is unavailable"):
            self.check()
        with self.assertRaisesRegex(checker.CommittedTextError, "observed ancestor"):
            self.check("push")

    def test_shallow_clone_fetches_only_trusted_origin_commits_and_rechecks(self):
        self.commit("docs/earlier.md", "earlier defect   \n")
        head = self.commit("docs/later.md", "clean latest\n")
        shallow = Path(self.temporary.name) / "shallow"
        self.git("clone", "--quiet", "--depth", "1", self.root.as_uri(), str(shallow))
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository", repo=shallow).stdout.strip(), "true")
        with self.assertRaisesRegex(checker.CommittedTextError, "Required commit object"):
            self.check(repo=shallow, head=head)
        with self.assertRaisesRegex(checker.CommittedTextError, "trailing whitespace"):
            self.check(repo=shallow, head=head, fetch=True)
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository", repo=shallow).stdout.strip(), "false")

    def test_failed_trusted_fetch_is_an_explicit_blocker(self):
        with self.assertRaisesRegex(checker.CommittedTextError, "Unable to fetch the exact trusted commits"):
            self.check(base="f" * 40, fetch=True)

    def test_invalid_refs_are_rejected_before_any_git_invocation(self):
        for bad in ("main", "HEAD^", "--upload-pack=bad", "a" * 40 + "\n", "a" * 40 + "; touch unsafe", "$(echo unsafe)"):
            with self.subTest(value=bad), mock.patch.object(checker, "_git") as git:
                with self.assertRaises(checker.CommittedTextError):
                    self.check(base=bad, head=self.base)
                git.assert_not_called()

    def test_local_whitespace_configuration_cannot_disable_the_check(self):
        self.git("config", "core.whitespace", "-blank-at-eol,-blank-at-eof,-space-before-tab")
        self.commit("docs/config.md", "bad whitespace   \n")
        with self.assertRaisesRegex(checker.CommittedTextError, "trailing whitespace"):
            self.check()


if __name__ == "__main__":
    unittest.main()
