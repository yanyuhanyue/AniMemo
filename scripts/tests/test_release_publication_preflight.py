"""Persistent CI coverage for the actual publication shell and CLI seams."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from release import test_metadata_freshness as freshness_fixtures
from release.candidate import (
    CandidateContractError,
    _validate_candidate_state_root_path,
)
from scripts.tests.test_distribution_portable import write_complete_portable_source
from scripts.tests.test_release_workflows import _bash_path, workflow

ROOT = Path(__file__).resolve().parents[2]


class PublicationShellBoundaryTests(unittest.TestCase):
    def test_archive_source_tree_uses_real_git_and_rejects_option_input(self):
        from scripts.release_publication_preflight import (
            _git_revision,
            _replay_environment,
            _source_commit,
        )

        with tempfile.TemporaryDirectory() as temporary:
            env = _replay_environment(Path(temporary))
            sha = _source_commit(env)
            tree = _git_revision(f"{sha}^{{tree}}", env)
            self.assertRegex(tree, r"^[0-9a-f]{40}$")
            for invalid in ("--help", "-c", "HEAD^{tree}", "x" * 40 + "^{tree}"):
                with mock.patch("subprocess.run") as run, self.assertRaises(ValueError):
                    _git_revision(invalid, env)
                run.assert_not_called()

    def test_replay_ignores_inherited_tools_and_shell_initialization(self):
        from scripts.release_publication_preflight import (
            _bash_executable,
            _replay_environment,
            _source_commit,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            hostile = root / "untrusted-jq-parent"
            hostile.mkdir()
            marker = root / "executed"
            injected = f"printf injected > '{marker.as_posix()}'\n"
            (hostile / "git").write_text("#!/bin/bash\n" + injected, encoding="utf-8")
            (hostile / "git").chmod(0o700)
            initializer = hostile / "init.sh"
            initializer.write_text(injected, encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": str(hostile),
                    "BASH_ENV": initializer.as_posix(),
                    "ENV": initializer.as_posix(),
                    "BASH_FUNC_git%%": "() { " + injected.strip() + "; }",
                },
            ):
                env = _replay_environment(tools)
                observed_commit = _source_commit(env)
            self.assertRegex(observed_commit, r"^[0-9a-f]{40}$")
            result = subprocess.run(
                [_bash_executable(), "--noprofile", "--norc", "-c", "git --version"],
                env=env,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith(b"git version "))
            self.assertFalse(marker.exists())
            self.assertNotIn(str(hostile), env["PATH"])
            self.assertFalse(
                any(k in env for k in ("BASH_ENV", "ENV", "BASH_FUNC_git%%"))
            )

    def test_untrusted_jq_is_rejected_without_staging_or_execution(self):
        from scripts.release_publication_preflight import _prepare_jq

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            fake = root / "jq.exe"
            fake.write_bytes(b"untrusted executable")
            with mock.patch("subprocess.run") as run, self.assertRaises(ValueError):
                _prepare_jq(fake, tools)
            run.assert_not_called()
            self.assertEqual(list(tools.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows pinned jq staging")
    def test_jq_staging_copies_only_verified_bytes_from_supplied_directory(self):
        from scripts import release_publication_preflight as preflight

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            payload = b"verified test fixture"
            source = root / "jq.exe"
            source.write_bytes(payload)
            (root / "git.exe").write_bytes(b"hostile sibling")
            with mock.patch.object(
                preflight, "WINDOWS_JQ_SHA256", hashlib.sha256(payload).hexdigest()
            ):
                preflight._prepare_jq(source, tools)
            source.write_bytes(b"subsequent replacement")
            self.assertEqual((tools / "jq.exe").read_bytes(), payload)
            self.assertEqual([p.name for p in tools.iterdir()], ["jq.exe"])

    def test_cli_cannot_choose_an_arbitrary_shell_executable(self):
        from scripts.release_publication_preflight import _bash_executable, main

        arguments = [
            "preflight",
            "--qualification-directory",
            "q",
            "--freshness-directory",
            "f",
            "--candidate-receipt",
            "receipt",
            "--output-directory",
            "output",
            "--jq",
            "jq",
            "--bash",
            "untrusted-program",
        ]
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch("subprocess.run") as run,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()
        self.assertIn(
            _bash_executable(), {"C:/Program Files/Git/bin/bash.exe", "/usr/bin/bash"}
        )

    def test_timeout_and_launch_failure_leave_terminal_stage_with_unknown_exit(self):
        from scripts.release_publication_preflight import (
            PreflightStageError,
            _stage_record,
        )

        for failure, code in (
            (subprocess.TimeoutExpired(["shell"], 900), "STAGE_TIMEOUT"),
            (OSError("private failure detail"), "STAGE_IO_FAILURE"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                summary = {
                    "context": "NON_AUTHORITATIVE_TEST",
                    "stages": [],
                    "remote_mutations": 0,
                }
                with (
                    mock.patch("subprocess.run", side_effect=failure),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(PreflightStageError),
                    _stage_record(summary, Path(temporary), "qualification"),
                ):
                    subprocess.run(["shell"], check=False)
                saved = json.loads((Path(temporary) / "result.json").read_bytes())
                self.assertEqual(saved["status"], "FAIL")
                self.assertEqual(saved["stages"][-1]["code"], code)
                self.assertIsNone(saved["stages"][-1]["exit_code"])

    def test_diagnostic_write_failure_preserves_known_primary_exit(self):
        from scripts.release_publication_preflight import (
            PreflightStageError,
            _stage_record,
        )

        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            summary = {"stages": []}
            with (
                self.assertRaises(PreflightStageError) as caught,
                _stage_record(summary, Path(temporary), "qualification") as record,
            ):
                record["exit_code"] = 7
                raise OSError("diagnostic failed")
            self.assertEqual(caught.exception.exit_code, 7)
            self.assertEqual(summary["stages"][-1]["code"], "STAGE_EXIT")
            self.assertEqual(
                summary["secondary_failures"], ["STAGE_DIAGNOSTIC_WRITE_FAILED"]
            )

    def test_replay_child_rejects_transaction_and_unlisted_mutation_modules(self):
        from scripts.release_publication_preflight import _python_child

        for arguments in (
            ["-m", "scripts.release_publication", "transaction-reconcile"],
            ["-m", "release.mirror", "--release-tag", "v2.0.0-rc.1"],
            ["-c", 'import os; os.remove("sentinel")'],
        ):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                self.assertEqual(_python_child(arguments), 2)

    def test_actual_workflow_passes_absolute_candidate_root(self):
        step = next(
            s["run"]
            for s in workflow("release.yml")["jobs"]["publish"]["steps"]
            if s.get("name")
            == "Verify and stage exact qualified prepublication materials before mutation"
        )
        assignment = next(
            line.strip()
            for line in step.splitlines()
            if line.strip().startswith("candidate_state=")
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [_bash_path(), "-c", assignment + '\nprintf "%s" "$candidate_state"'],
                cwd=temporary,
                capture_output=True,
                check=True,
            )
        observed = result.stdout.decode("utf-8")
        _validate_candidate_state_root_path(PurePosixPath(observed))
        with self.assertRaises(CandidateContractError):
            _validate_candidate_state_root_path(
                PurePosixPath("validated-release-input/verified-candidate-state")
            )
        _validate_candidate_state_root_path(PureWindowsPath("E:/verified-state"))

    def test_actual_diagnostic_reports_code_without_command_or_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env["RUNNER_TEMP"] = Path(temporary).as_posix()
            script = "set -euo pipefail\nsource scripts/release-input-diagnostics.sh\nrelease_input_stage MATERIALS\nfalse\n"
            result = subprocess.run(
                [_bash_path(), "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            records = [
                json.loads(line)
                for line in (Path(temporary) / "release-input-diagnostics.jsonl")
                .read_text("utf-8")
                .splitlines()
            ]
        self.assertEqual(records[-1]["stage"], "MATERIALS")
        self.assertEqual(records[-1]["exit_code"], 1)
        self.assertEqual(records[-1]["status"], "FAIL")
        self.assertEqual(
            set(records[-1]), {"schema", "stage", "status", "exit_code", "line"}
        )
        self.assertNotIn(b"false", result.stderr)

    @unittest.skipUnless(
        shutil.which("jq"), "jq is exercised by the Linux scripts CI job"
    )
    def test_receipt_is_masked_before_workflow_env_exposure(self):
        # No actual wire is printed by this test process: compare only hashes.
        from release.candidate import encode_aggregate_receipt_b64url
        from scripts import (
            candidate_receipt_regression as regression,
        )
        from scripts import (
            candidate_vm_harness as h,
        )

        source = regression.load_fixture(
            ROOT / "scripts/tests/fixtures/candidate-wire-real-scale.json.xz"
        )
        receipt = h.build_candidate_aggregate(
            regression._scope(source),
            profile_results=source["profileResults"],
            receipts=source["profileReceipts"],
            candidate_prestate=source["candidatePrestate"],
            candidate_poststate=source["candidatePrestate"],
            r2_prestate_receipt=source["r2OriginPrestateReceipt"],
            r2_poststate_receipt=source["r2OriginPoststateReceipt"],
            plugin_origin=True,
        )
        wire = encode_aggregate_receipt_b64url(receipt)
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(
                json.dumps({"inputs": {"candidate_acceptance_receipt_b64url": wire}}),
                encoding="utf-8",
            )
            env = {**os.environ, "GITHUB_EVENT_PATH": event.as_posix()}
            done = subprocess.run(
                [_bash_path(), "scripts/mask-candidate-receipt.sh"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                check=False,
            )
            self.assertEqual(done.returncode, 0)
            self.assertEqual(
                hashlib.sha256(done.stdout).hexdigest(),
                hashlib.sha256(("::add-mask::" + wire + "\n").encode()).hexdigest(),
            )
        for filename in ("release.yml", "release-metadata-freshness.yml"):
            for job in workflow(filename)["jobs"].values():
                steps = job.get("steps", [])
                wire_steps = [
                    i
                    for i, s in enumerate(steps)
                    if "candidate_acceptance_receipt_b64url" in json.dumps(s)
                ]
                if wire_steps:
                    masks = [
                        i
                        for i, s in enumerate(steps)
                        if s.get("run") == "bash scripts/mask-candidate-receipt.sh"
                    ]
                    self.assertTrue(masks and masks[0] < min(wire_steps))


class PublicationPlanV3CliTests(unittest.TestCase):
    def setUp(self):
        fixture = freshness_fixtures.MetadataFreshnessTests()
        fixture.setUp()
        self.addCleanup(fixture.temporary.cleanup)
        self.fixture = fixture
        self.root = fixture.root
        self.qualification = (
            fixture.qualification
            / f"release-qualification-{freshness_fixtures.QUALIFICATION_RUN_ID}.json"
        )
        self.q = json.loads(self.qualification.read_bytes())
        self.assets = self.root / "assets"
        self.assets.mkdir()
        source = self.root / "portable-source"
        source.mkdir()
        self.images = write_complete_portable_source(source)
        for name in (
            "release-manifest.json",
            "deployment-contract.json",
            "installer-materials.tar",
            "checksums.txt",
        ):
            shutil.copyfile(source / "authority" / name, self.assets / name)
        self.portable = self.assets / f"animemo-{self.q['release_tag']}-portable.tar"
        command = [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "release.cli",
            "build-portable",
            "--source-root",
            str(source),
            "--output",
            str(self.portable),
        ]
        for image in self.images:
            command += [
                "--image",
                f"{image['role']}={image['repository']}@{image['digest']}",
            ]
        done = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8"))

    def command(self, output):
        images = {i["role"]: i for i in self.images}
        return [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "release.cli",
            "plan-publication-files",
            "--repository",
            "yanyuhanyue/AniMemo",
            "--channel",
            "rc",
            "--tag",
            self.q["release_tag"],
            "--commit",
            self.q["candidate_sha"],
            "--qualification",
            str(self.qualification),
            "--release-notes",
            str(self.fixture.qualification / "release-notes.json"),
            "--release-notes-markdown",
            str(self.fixture.qualification / "release-notes.md"),
            "--asset-directory",
            str(self.assets),
            "--portable",
            str(self.portable),
            "--api-digest",
            images["api"]["digest"],
            "--web-digest",
            images["web"]["digest"],
            "--output",
            str(output),
        ]

    def test_full_v3_qualification_and_real_portable_reach_cli_without_projection(self):
        before = self.qualification.read_bytes()
        output = self.root / "plan.json"
        result = subprocess.run(
            self.command(output), cwd=ROOT, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        plan = json.loads(output.read_bytes())
        self.assertEqual(plan["qualification_identity"], self.q["qualification_sha256"])
        self.assertEqual(self.qualification.read_bytes(), before)
        self.assertEqual(plan["external_mutation_mode"], "PLAN_ONLY")

    def test_tampered_qualification_fails_before_output(self):
        changed = dict(self.q)
        changed["candidate_sha"] = "f" * 40
        self.qualification.write_text(json.dumps(changed), encoding="utf-8")
        output = self.root / "invalid-plan.json"
        result = subprocess.run(
            self.command(output), cwd=ROOT, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())
        self.assertNotIn(b"Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
