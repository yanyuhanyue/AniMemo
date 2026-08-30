from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .acceptance import verify_stable_promotion_acceptance
from .formal_acceptance_test_support import build_test_formal_acceptance
from .notes import CANONICAL_RELEASE_ASSETS
from .presentation import (
    PresentationError,
    ReleasePresentationIdentity,
    presentation_identity_from_publication_plan,
    presentation_identity_from_stable_plan,
    validate_release_presentation_identity,
    verify_local_annotated_tag,
    verify_release_presentation_metadata,
    verify_stable_source_rc_presentation,
)
from .publication import build_publication_plan

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "sha256:" + "1" * 64
API_DIGEST = "sha256:" + "2" * 64
WEB_DIGEST = "sha256:" + "3" * 64


def _plan(*, tag: str, commit: str, channel: str = "rc") -> dict[str, object]:
    assets = {
        name: {
            "sha256": "sha256:" + hashlib.sha256(name.encode("ascii")).hexdigest(),
            "size": len(name),
        }
        for name in CANONICAL_RELEASE_ASSETS
    }
    portable = f"animemo-{tag}-portable.tar"
    return build_publication_plan(
        repository="yanyuhanyue/AniMemo",
        channel=channel,
        tag=tag,
        commit=commit,
        qualification_identity=IDENTITY,
        release_notes_identity=IDENTITY,
        release_notes_markdown_sha256=IDENTITY,
        assets=assets,
        transport_assets={
            portable: {
                "role": "PORTABLE_RELEASE_BUNDLE",
                "sha256": IDENTITY,
                "size": 1,
            }
        },
        api_digest=API_DIGEST,
        web_digest=WEB_DIGEST,
    )


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def _repository(root: Path) -> tuple[Path, str]:
    repository = root / "work"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Presentation Test")
    _git(repository, "config", "user.email", "presentation@example.invalid")
    (repository / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(repository, "add", "candidate.txt")
    _git(repository, "commit", "-m", "候选提交")
    return repository, _git(repository, "rev-parse", "HEAD")


def _tag(repository: Path, tag: str, commit: str, message: str) -> None:
    _git(repository, "tag", "--annotate", tag, commit, "--message", message)


def _metadata(tag: str, *, title: str | None = None, stable: bool = False):
    return {
        "id": 1,
        "tag_name": tag,
        "name": title if title is not None else tag,
        "target_commitish": "main",
        "draft": True,
        "prerelease": not stable,
        "immutable": False,
        "assets": [],
    }


def _acceptance(tag: str, commit: str):
    return build_test_formal_acceptance(
        rc_tag=tag,
        rc_commit=commit,
        rc_tree=commit,
        release_manifest_identity=IDENTITY,
        deployment_contract_identity=IDENTITY,
        installer_materials_identity=IDENTITY,
        api_digest=API_DIGEST,
        web_digest=WEB_DIGEST,
        fresh_base_identity=IDENTITY,
        docker_base_identity=IDENTITY,
        runtime_base_identity=IDENTITY,
        accepted_at="2026-08-23T00:00:00Z",
        operator_identity="presentation-test",
        tool_identity=IDENTITY,
    )


def _promotion(acceptance):
    return verify_stable_promotion_acceptance(
        acceptance,
        expected={
            field: acceptance[field]
            for field in (
                "rc_tag",
                "rc_commit",
                "release_manifest_identity",
                "deployment_contract_identity",
                "installer_materials_identity",
                "api_digest",
                "web_digest",
            )
        },
        stable_commit=acceptance["rc_commit"],
        stable_api_digest=acceptance["api_digest"],
        stable_web_digest=acceptance["web_digest"],
    )


class _StubGh:
    def __init__(self, draft_metadata):
        self.draft_metadata = draft_metadata
        self.commands = []
        self.asset_upload_count = 0
        self.release_publish_count = 0

    def create_draft(self, *, tag: str, title: str):
        self.commands.append(["gh", "release", "create", tag, "--title", title])
        return copy.deepcopy(self.draft_metadata)

    def upload_assets(self):
        self.asset_upload_count += 1

    def publish(self):
        self.release_publish_count += 1


class _StubStablePublisher:
    def __init__(self):
        self.commands = []

    def create_tag(self, tag: str):
        self.commands.append(["git", "tag", "--annotate", tag])

    def create_release(self, tag: str):
        self.commands.append(["gh", "release", "create", tag])


def _simulate_rc_transaction(repository: Path, plan, gh: _StubGh):
    identity = presentation_identity_from_publication_plan(plan)
    tag_receipt = verify_local_annotated_tag(
        repository,
        identity=identity,
        expected_commit=plan["commit"],
    )
    push_argv = ["push", "origin", f"refs/tags/{identity.release_tag}"]
    commands = [["git", *push_argv]]
    _git(repository, *push_argv)
    draft = gh.create_draft(
        tag=identity.release_tag,
        title=identity.release_title,
    )
    commands.extend(gh.commands)
    draft_receipt = verify_release_presentation_metadata(
        plan,
        metadata=draft,
        repository=repository,
        state="draft",
    )
    gh.upload_assets()
    return commands, tag_receipt, draft_receipt


def _simulate_stable_source_transaction(
    *, repository: Path, release, acceptance, promotion_acceptance, publisher
):
    receipt = verify_stable_source_rc_presentation(
        release=release,
        acceptance=acceptance,
        promotion_acceptance=promotion_acceptance,
        repository=repository,
    )
    stable_tag = "v1.1.0"
    publisher.create_tag(stable_tag)
    publisher.create_release(stable_tag)
    return receipt


class PresentationIdentityTests(unittest.TestCase):
    def test_rc_and_stable_project_through_the_same_closed_identity(self):
        rc = presentation_identity_from_publication_plan(
            _plan(tag="v1.1.0-rc.8", commit="a" * 40)
        )
        stable = presentation_identity_from_stable_plan(
            _plan(tag="v1.1.0", commit="a" * 40, channel="stable")
        )
        self.assertEqual(
            rc,
            ReleasePresentationIdentity("v1.1.0-rc.8", "v1.1.0-rc.8", "v1.1.0-rc.8"),
        )
        self.assertEqual(
            stable,
            ReleasePresentationIdentity("v1.1.0", "v1.1.0", "v1.1.0"),
        )
        self.assertIs(validate_release_presentation_identity(rc), rc)
        self.assertIs(validate_release_presentation_identity(stable), stable)

    def test_arbitrary_title_subject_whitespace_control_and_lookalike_fail(self):
        tag = "v1.1.0-rc.8"
        invalid = (
            ("AniMemo v1.1.0-rc.7", tag),
            (tag, "AniMemo v1.1.0-rc.7"),
            ("Release v1.1.0-rc.8", tag),
            (" " + tag, tag),
            (tag + " ", tag),
            (tag + "\n", tag),
            (tag, tag + "\nbody"),
            (tag, tag + "\x00"),
            (tag + "-suffix", tag),
            ("ｖ1.1.0-rc.8", "ｖ1.1.0-rc.8"),
        )
        for title, subject in invalid:
            with (
                self.subTest(title=title, subject=subject),
                self.assertRaises(PresentationError),
            ):
                validate_release_presentation_identity(
                    ReleasePresentationIdentity(tag, title, subject)
                )

    def test_identity_mapping_is_closed(self):
        with self.assertRaises(PresentationError):
            validate_release_presentation_identity(
                {
                    "release_tag": "v1.1.0-rc.8",
                    "release_title": "v1.1.0-rc.8",
                    "annotated_tag_subject": "v1.1.0-rc.8",
                    "override": "AniMemo v1.1.0-rc.8",
                }
            )

        for tag in ("v1.1.0-rc.TEST", "v1.1.0-beta.TEST"):
            with self.subTest(tag=tag), self.assertRaises(PresentationError):
                validate_release_presentation_identity(
                    ReleasePresentationIdentity(tag, tag, tag)
                )

    def test_plan_commands_are_cross_checked_but_never_executed(self):
        plan = _plan(tag="v1.1.0-rc.8", commit="a" * 40)
        marker = ROOT / "presentation-command-must-not-run"
        tampered = copy.deepcopy(plan)
        tampered["commands"]["create_tag"] = [
            "python",
            "-c",
            f"open({str(marker)!r}, 'w').write('executed')",
        ]
        with self.assertRaises(ValueError):
            presentation_identity_from_publication_plan(tampered)
        self.assertFalse(marker.exists())


class PresentationGuardTests(unittest.TestCase):
    def test_local_annotated_tag_exact_subject_empty_body_and_commit_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = _repository(Path(directory))
            tag = "v1.1.0-rc.8"
            _tag(repository, tag, commit, tag)
            receipt = verify_local_annotated_tag(
                repository,
                identity=ReleasePresentationIdentity(tag, tag, tag),
                expected_commit=commit,
            )
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["annotated_tag_body"], "")

    def test_local_tag_rejects_prefix_body_and_wrong_commit_before_push(self):
        cases = (
            ("AniMemo v1.1.0-rc.8", None),
            ("v1.1.0-rc.8\n\nbody", None),
            ("v1.1.0-rc.8", "b" * 40),
        )
        for message, expected_override in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                repository, commit = _repository(Path(directory))
                tag = "v1.1.0-rc.8"
                _tag(repository, tag, commit, message)
                with self.assertRaises(PresentationError):
                    verify_local_annotated_tag(
                        repository,
                        identity=ReleasePresentationIdentity(tag, tag, tag),
                        expected_commit=expected_override or commit,
                    )

    def test_draft_preasset_guard_is_closed_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = _repository(Path(directory))
            tag = "v1.1.0-rc.8"
            plan = _plan(tag=tag, commit=commit)
            _tag(repository, tag, commit, tag)
            receipt = verify_release_presentation_metadata(
                plan,
                metadata=_metadata(tag),
                repository=repository,
                state="draft",
            )
            self.assertEqual(receipt["asset_count"], 0)
            mutations = (
                {"name": "AniMemo v1.1.0-rc.8"},
                {"tag_name": "v1.1.0-rc.9"},
                {"prerelease": False},
                {"draft": False},
                {"immutable": True},
                {"assets": [{"name": "unexpected"}]},
            )
            for changes in mutations:
                metadata = {**_metadata(tag), **changes}
                with (
                    self.subTest(changes=changes),
                    self.assertRaises(PresentationError),
                ):
                    verify_release_presentation_metadata(
                        plan,
                        metadata=metadata,
                        repository=repository,
                        state="draft",
                    )

    def test_post_publish_guard_still_checks_title_and_tag_subject(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = _repository(Path(directory))
            tag = "v1.1.0-rc.8"
            plan = _plan(tag=tag, commit=commit)
            _tag(repository, tag, commit, tag)
            published = {
                **_metadata(tag),
                "draft": False,
                "immutable": True,
                "assets": [{"name": name} for name in CANONICAL_RELEASE_ASSETS],
            }
            self.assertEqual(
                verify_release_presentation_metadata(
                    plan,
                    metadata=published,
                    repository=repository,
                    state="published",
                )["status"],
                "PASS",
            )
            published["name"] = "AniMemo v1.1.0-rc.8"
            with self.assertRaises(PresentationError):
                verify_release_presentation_metadata(
                    plan,
                    metadata=published,
                    repository=repository,
                    state="published",
                )

    def test_rc7_real_mismatch_fixture_is_rejected_for_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = _repository(Path(directory))
            tag = "v1.1.0-rc.7"
            _tag(repository, tag, commit, "AniMemo v1.1.0-rc.7")
            acceptance = _acceptance(tag, commit)
            release = {
                "id": 375130669,
                "tag_name": tag,
                "name": "AniMemo v1.1.0-rc.7",
                "draft": False,
                "prerelease": True,
                "immutable": True,
            }
            with self.assertRaises(PresentationError) as caught:
                verify_stable_source_rc_presentation(
                    release=release,
                    acceptance=acceptance,
                    promotion_acceptance=_promotion(acceptance),
                    repository=repository,
                )
            self.assertEqual(
                caught.exception.code,
                "SOURCE_RC_PRESENTATION_AUTHORITY_MISMATCH",
            )

    def test_correct_rc_presentation_can_enter_stable_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = _repository(Path(directory))
            tag = "v1.1.0-rc.8"
            _tag(repository, tag, commit, tag)
            acceptance = _acceptance(tag, commit)
            receipt = verify_stable_source_rc_presentation(
                release={
                    "id": 1,
                    "tag_name": tag,
                    "name": tag,
                    "draft": False,
                    "prerelease": True,
                    "immutable": True,
                },
                acceptance=acceptance,
                promotion_acceptance=_promotion(acceptance),
                repository=repository,
            )
            self.assertEqual(receipt["status"], "PASS")


class LocalPublicationTransactionSimulationTests(unittest.TestCase):
    def _with_remote(self, root: Path):
        repository, commit = _repository(root)
        remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            capture_output=True,
        )
        _git(repository, "remote", "add", "origin", str(remote))
        return repository, remote, commit

    def test_success_and_three_failures_gate_each_mutation_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, _remote, commit = self._with_remote(Path(directory))
            tag = "v1.1.0-rc.8"
            plan = _plan(tag=tag, commit=commit)
            identity = presentation_identity_from_publication_plan(plan)
            _tag(repository, tag, commit, identity.annotated_tag_subject)
            gh = _StubGh(_metadata(tag))
            commands, tag_receipt, draft_receipt = _simulate_rc_transaction(
                repository,
                plan,
                gh,
            )
            self.assertEqual(commands[0], ["git", "push", "origin", f"refs/tags/{tag}"])
            self.assertEqual(commands[1][-1], tag)
            self.assertEqual(tag_receipt["status"], "PASS")
            self.assertEqual(draft_receipt["status"], "PASS")
            self.assertEqual(gh.asset_upload_count, 1)

        with tempfile.TemporaryDirectory() as directory:
            repository, remote, commit = self._with_remote(Path(directory))
            tag = "v1.1.0-rc.8"
            plan = _plan(tag=tag, commit=commit)
            _tag(repository, tag, commit, "AniMemo v1.1.0-rc.8")
            gh = _StubGh(_metadata(tag))
            with self.assertRaises(PresentationError) as caught:
                _simulate_rc_transaction(repository, plan, gh)
            self.assertEqual(caught.exception.code, "LOCAL_TAG_PRESENTATION_MISMATCH")
            remote_ref = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(remote),
                    "show-ref",
                    "--verify",
                    f"refs/tags/{tag}",
                ],
                check=False,
                capture_output=True,
            )
            self.assertNotEqual(remote_ref.returncode, 0)
            self.assertEqual(gh.commands, [])

        with tempfile.TemporaryDirectory() as directory:
            repository, _remote, commit = self._with_remote(Path(directory))
            tag = "v1.1.0-rc.8"
            plan = _plan(tag=tag, commit=commit)
            _tag(repository, tag, commit, tag)
            gh = _StubGh(_metadata(tag, title="AniMemo v1.1.0-rc.8"))
            with self.assertRaises(PresentationError) as caught:
                _simulate_rc_transaction(repository, plan, gh)
            self.assertEqual(
                caught.exception.code,
                "PARTIAL_DRAFT_RELEASE_TRANSACTION",
            )
            self.assertEqual(len(gh.commands), 1)
            self.assertEqual((gh.asset_upload_count, gh.release_publish_count), (0, 0))

        with tempfile.TemporaryDirectory() as directory:
            repository, commit = _repository(Path(directory))
            tag = "v1.1.0-rc.7"
            _tag(repository, tag, commit, "AniMemo v1.1.0-rc.7")
            acceptance = _acceptance(tag, commit)
            publisher = _StubStablePublisher()
            with self.assertRaises(PresentationError) as caught:
                _simulate_stable_source_transaction(
                    repository=repository,
                    release={
                        "id": 375130669,
                        "tag_name": tag,
                        "name": "AniMemo v1.1.0-rc.7",
                        "draft": False,
                        "prerelease": True,
                        "immutable": True,
                    },
                    acceptance=acceptance,
                    promotion_acceptance=_promotion(acceptance),
                    publisher=publisher,
                )
            self.assertEqual(
                caught.exception.code,
                "SOURCE_RC_PRESENTATION_AUTHORITY_MISMATCH",
            )
            self.assertEqual(publisher.commands, [])


class PresentationCliTests(unittest.TestCase):
    def _run(self, cwd: Path, *arguments: str):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        return subprocess.run(
            [sys.executable, "-m", "release.cli", *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_reads_only_workspace_plan_and_emits_multiline_safe_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            plan = workspace / "plan.json"
            plan.write_text(
                json.dumps(_plan(tag="v1.1.0-rc.8", commit="a" * 40)),
                encoding="utf-8",
            )
            output = workspace / "github-output"
            output.touch()
            completed = self._run(
                workspace,
                "emit-publication-presentation",
                "--plan",
                "plan.json",
                "--github-output",
                str(output),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            text = output.read_text(encoding="utf-8")
            self.assertEqual(text.count("<<ANIMEMO_PRESENTATION_"), 3)
            self.assertIn("release_tag<<", text)
            self.assertIn("release_title<<", text)
            self.assertIn("annotated_tag_subject<<", text)
            self.assertNotIn("create_tag", text)
            self.assertNotIn("commands", text)

    def test_cli_rejects_outside_duplicate_key_and_hardlinked_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "outside.json"
            outside.write_text(
                json.dumps(_plan(tag="v1.1.0-rc.8", commit="a" * 40)),
                encoding="utf-8",
            )
            output = workspace / "github-output"
            output.touch()
            outside_result = self._run(
                workspace,
                "emit-publication-presentation",
                "--plan",
                str(outside),
                "--github-output",
                str(output),
            )
            self.assertEqual(outside_result.returncode, 2)

            duplicate = workspace / "duplicate.json"
            duplicate.write_text('{"schema":"one","schema":"two"}', encoding="utf-8")
            duplicate_result = self._run(
                workspace,
                "emit-publication-presentation",
                "--plan",
                str(duplicate),
                "--github-output",
                str(output),
            )
            self.assertEqual(duplicate_result.returncode, 2)

            hardlink = workspace / "hardlink.json"
            os.link(outside, hardlink)
            hardlink_result = self._run(
                workspace,
                "emit-publication-presentation",
                "--plan",
                str(hardlink),
                "--github-output",
                str(output),
            )
            self.assertEqual(hardlink_result.returncode, 2)

    def test_cli_reports_transaction_specific_failure_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, commit = _repository(Path(directory))
            tag = "v1.1.0-rc.8"
            plan = repository / "plan.json"
            plan.write_text(
                json.dumps(_plan(tag=tag, commit=commit)),
                encoding="utf-8",
            )
            _tag(repository, tag, commit, "AniMemo v1.1.0-rc.8")
            local = self._run(
                repository,
                "verify-local-tag-presentation",
                "--plan",
                "plan.json",
                "--repository",
                ".",
            )
            self.assertEqual(local.returncode, 2)
            self.assertEqual(
                json.loads(local.stderr)["code"],
                "LOCAL_TAG_PRESENTATION_MISMATCH",
            )

            _git(repository, "tag", "--delete", tag)
            _tag(repository, tag, commit, tag)
            metadata = repository / "metadata.json"
            metadata.write_text(
                json.dumps(_metadata(tag, title="AniMemo v1.1.0-rc.8")),
                encoding="utf-8",
            )
            draft = self._run(
                repository,
                "verify-release-presentation",
                "--plan",
                "plan.json",
                "--metadata",
                "metadata.json",
                "--repository",
                ".",
                "--state",
                "draft",
            )
            self.assertEqual(draft.returncode, 2)
            self.assertEqual(
                json.loads(draft.stderr)["code"],
                "PARTIAL_DRAFT_RELEASE_TRANSACTION",
            )


if __name__ == "__main__":
    unittest.main()
