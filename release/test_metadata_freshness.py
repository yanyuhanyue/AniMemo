from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from release.metadata_freshness import (
    ARTIFACT_FILES,
    DIAGNOSTIC_FIELDS,
    ENDPOINT_TEMPLATE,
    WORKFLOW_PATH,
    FetchResponse,
    FreshnessExpectation,
    FreshnessRunIdentity,
    GitHubAssociatedPullSource,
    MetadataFreshnessError,
    _receipt_digest,
    _validate_receipt,
    collect_metadata_freshness,
    extract_metadata_freshness_artifact,
    verify_metadata_freshness_artifact,
)
from release.notes import build_release_notes, render_release_notes
from scripts.release_qualification import REQUIRED_GATES, build_qualification_evidence

CANDIDATE = "a" * 40
TREE = "b" * 40
BASE = "c" * 40
COMMIT_ONE = "d" * 40
COMMIT_TWO = "e" * 40
QUALIFICATION_RUN_ID = 123
QUALIFICATION_ARTIFACT_ID = 456
FRESHNESS_RUN_ID = 789


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _pull(title: str = "修复发布门禁", *, number: int = 45) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "merge_commit_sha": "f" * 40,
        "head": {"sha": "1" * 40},
        "labels": [{"name": "release/fix"}],
    }


def _normalized_pull(title: str = "修复发布门禁", *, number: int = 45) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "source_identity": "f" * 40,
        "labels": ["release/fix"],
    }


def _context() -> dict[str, object]:
    return {
        "candidate_sha": CANDIDATE,
        "comparison_base_sha": BASE,
        "previous_stable": "v1.0.0",
        "release_tag": "v1.1.0-rc.9",
        "target_version": "v1.1.0",
        "channel": "rc",
        "minimum_updater_version": "v1.0.0",
        "supported_os": ["Ubuntu 24.04 LTS"],
        "docker_requirement": "Docker Engine 28+",
        "release_assets": [
            "release-manifest.json",
            "deployment-contract.json",
            "installer-materials.tar",
            "checksums.txt",
        ],
    }


class FakeClock:
    def __init__(self, *, advance_sleep: bool = True) -> None:
        self.current = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        self.elapsed = 0.0
        self.advance_sleep = advance_sleep

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        if self.advance_sleep:
            self.current += timedelta(seconds=seconds)
            self.elapsed += seconds


def _diagnostic(
    *, snapshot: str, attempt: int, commit: str, code: str | None = None
) -> dict[str, object]:
    value = {field: None for field in DIAGNOSTIC_FIELDS}
    value.update(
        {
            "endpointTemplate": ENDPOINT_TEMPLATE,
            "commitSha": commit,
            "snapshot": snapshot,
            "attempt": attempt,
            "startedAt": "2026-08-23T12:00:00Z",
            "durationMs": 1.0,
            "exitCode": 0 if code is None else 1,
            "httpStatus": 200 if code is None else None,
            "contentType": "application/json",
            "responseBytes": 200,
            "arrayLength": 1 if code is None else None,
            "sanitizedErrorClass": code,
            "result": "PASS" if code is None else "FAIL",
        }
    )
    return value


class StubSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.failures: dict[tuple[str, int, str], tuple[str, int | None]] = {}
        self.titles = {"A": "修复发布门禁", "B": "修复发布门禁"}

    def fetch(
        self, *, repository: str, commit: str, snapshot: str, attempt: int
    ) -> FetchResponse:
        self.calls.append((snapshot, attempt, commit))
        failure = self.failures.get((snapshot, attempt, commit))
        if failure:
            code, retry_after = failure
            return FetchResponse(
                pulls=None,
                diagnostic=_diagnostic(
                    snapshot=snapshot, attempt=attempt, commit=commit, code=code
                ),
                error_code=code,
                retry_after_seconds=retry_after,
            )
        return FetchResponse(
            pulls=[_pull(self.titles[snapshot])],
            diagnostic=_diagnostic(
                snapshot=snapshot, attempt=attempt, commit=commit
            ),
        )


class MetadataFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.qualification = self.root / "qualification"
        self.qualification.mkdir()
        self.output = self.root / "freshness"
        self.notes = build_release_notes(
            context=_context(), pulls=[_normalized_pull()]
        )
        markdown = render_release_notes(self.notes).encode()
        needs = {name: {"result": "success"} for name in REQUIRED_GATES}
        needs.update(
            {
                "release-authority": {"result": "success"},
                "dry-run": {"result": "success"},
            }
        )
        evidence = build_qualification_evidence(
            workflow_ref=(
                "yanyuhanyue/AniMemo/.github/workflows/release.yml@refs/heads/main"
            ),
            workflow_sha=CANDIDATE,
            run_id=str(QUALIFICATION_RUN_ID),
            run_attempt=1,
            candidate_sha=CANDIDATE,
            upgrade_base_sha=BASE,
            channel="rc",
            target_version="v1.1.0",
            release_tag="v1.1.0-rc.9",
            needs=needs,
            release_notes_identity=self.notes["identity"],
            release_notes_markdown_sha256=(
                "sha256:" + hashlib.sha256(markdown).hexdigest()
            ),
        )
        files = {
            f"release-qualification-{QUALIFICATION_RUN_ID}.json": _json_bytes(evidence),
            "platform-qualification.json": b"{}\n",
            "release-notes.json": _json_bytes(self.notes),
            "release-notes.md": markdown,
            "prepublication-materials.json": _json_bytes(
                {
                    "schemaVersion": 2,
                    "candidateSha": CANDIDATE,
                    "candidateTreeSha": TREE,
                }
            ),
            "installer-materials.tar": b"installer",
            "deployment-contract.json": b"{}\n",
        }
        for name, value in files.items():
            (self.qualification / name).write_bytes(value)
        self.identity = FreshnessRunIdentity(
            workflow_run_id=FRESHNESS_RUN_ID,
            workflow_attempt=1,
            workflow_path=WORKFLOW_PATH,
            workflow_sha=CANDIDATE,
            candidate_sha=CANDIDATE,
            candidate_tree=TREE,
            qualification_run_id=QUALIFICATION_RUN_ID,
            qualification_artifact_id=QUALIFICATION_ARTIFACT_ID,
        )
        self.expectation = FreshnessExpectation(
            workflow_run_id=FRESHNESS_RUN_ID,
            candidate_sha=CANDIDATE,
            candidate_tree=TREE,
            qualification_run_id=QUALIFICATION_RUN_ID,
            qualification_artifact_id=QUALIFICATION_ARTIFACT_ID,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _commits(_root: Path, _start: str, _candidate: str) -> list[str]:
        return [COMMIT_ONE, COMMIT_TWO]

    def _collect(
        self, source: StubSource | None = None, clock: FakeClock | None = None
    ) -> tuple[StubSource, FakeClock]:
        source = source or StubSource()
        clock = clock or FakeClock()
        collect_metadata_freshness(
            repository_root=self.repository,
            qualification_directory=self.qualification,
            output_directory=self.output,
            identity=self.identity,
            source=source,
            clock=clock,
            commit_loader=self._commits,
        )
        return source, clock

    def test_two_complete_identical_snapshots_pass_and_verify(self) -> None:
        source, clock = self._collect()
        self.assertEqual({path.name for path in self.output.iterdir()}, ARTIFACT_FILES)
        self.assertEqual(
            source.calls,
            [
                ("A", 1, COMMIT_ONE),
                ("A", 1, COMMIT_TWO),
                ("B", 1, COMMIT_ONE),
                ("B", 1, COMMIT_TWO),
            ],
        )
        result = verify_metadata_freshness_artifact(
            artifact_directory=self.output,
            qualification_directory=self.qualification,
            expectation=self.expectation,
            verified_at=clock.now() + timedelta(minutes=1),
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["snapshotIntervalSeconds"], 60)

    def test_interval_shorter_than_sixty_seconds_fails(self) -> None:
        with self.assertRaisesRegex(MetadataFreshnessError, "shorter than 60"):
            self._collect(clock=FakeClock(advance_sleep=False))

    def test_one_success_and_one_nonretryable_failure_fails(self) -> None:
        source = StubSource()
        source.failures[("B", 1, COMMIT_ONE)] = ("PERMISSION_FAILURE", None)
        with self.assertRaisesRegex(MetadataFreshnessError, "PERMISSION_FAILURE"):
            self._collect(source)
        self.assertFalse(self.output.exists())

    def test_metadata_drift_between_snapshots_fails(self) -> None:
        source = StubSource()
        source.titles["B"] = "发布元数据发生变化"
        with self.assertRaisesRegex(MetadataFreshnessError, "snapshots differ"):
            self._collect(source)

    def test_current_identity_or_population_different_from_qualification_fails(self) -> None:
        source = StubSource()
        source.titles = {"A": "新标题", "B": "新标题"}
        with self.assertRaisesRegex(MetadataFreshnessError, "differs from qualification"):
            self._collect(source)

    def test_transient_failure_restarts_the_whole_snapshot(self) -> None:
        source = StubSource()
        source.failures[("A", 1, COMMIT_TWO)] = ("TRANSPORT_EOF", None)
        self._collect(source)
        self.assertEqual(
            source.calls[:4],
            [
                ("A", 1, COMMIT_ONE),
                ("A", 1, COMMIT_TWO),
                ("A", 2, COMMIT_ONE),
                ("A", 2, COMMIT_TWO),
            ],
        )

    def test_http_429_is_bounded_to_three_complete_attempts(self) -> None:
        source = StubSource()
        for attempt in (1, 2, 3):
            source.failures[("A", attempt, COMMIT_ONE)] = (
                "SECONDARY_RATE_LIMIT",
                1,
            )
        with self.assertRaises(MetadataFreshnessError):
            self._collect(source)
        self.assertEqual(source.calls, [("A", attempt, COMMIT_ONE) for attempt in (1, 2, 3)])

    def test_http_401_and_permission_403_are_not_retried(self) -> None:
        for code in ("AUTHENTICATION_FAILURE", "PERMISSION_FAILURE"):
            with self.subTest(code=code):
                output = self.root / f"freshness-{code}"
                source = StubSource()
                source.failures[("A", 1, COMMIT_ONE)] = (code, None)
                with self.assertRaises(MetadataFreshnessError):
                    collect_metadata_freshness(
                        repository_root=self.repository,
                        qualification_directory=self.qualification,
                        output_directory=output,
                        identity=self.identity,
                        source=source,
                        clock=FakeClock(),
                        commit_loader=self._commits,
                    )
                self.assertEqual(source.calls, [("A", 1, COMMIT_ONE)])

    def test_connection_reset_and_eof_are_bounded(self) -> None:
        for code in ("TRANSPORT_CONNECTION_RESET", "TRANSPORT_EOF"):
            with self.subTest(code=code):
                output = self.root / f"freshness-{code}"
                source = StubSource()
                for attempt in (1, 2, 3):
                    source.failures[("A", attempt, COMMIT_ONE)] = (code, None)
                with self.assertRaises(MetadataFreshnessError):
                    collect_metadata_freshness(
                        repository_root=self.repository,
                        qualification_directory=self.qualification,
                        output_directory=output,
                        identity=self.identity,
                        source=source,
                        clock=FakeClock(),
                        commit_loader=self._commits,
                    )
                self.assertEqual(len(source.calls), 3)

    def test_closed_receipt_rejects_unknown_conflict_unclassified_and_duplicate(self) -> None:
        self._collect()
        receipt = json.loads((self.output / "metadata-freshness.json").read_text())
        cases = []
        unknown = copy.deepcopy(receipt)
        unknown["override"] = True
        cases.append(unknown)
        for field in ("conflicts", "unclassified", "duplicates"):
            changed = copy.deepcopy(receipt)
            changed[field] = 1
            changed["receiptDigest"] = _receipt_digest(changed)
            cases.append(changed)
        for value in cases:
            with self.subTest(keys=set(value)), self.assertRaises(
                MetadataFreshnessError
            ):
                _validate_receipt(value)

    def test_duplicate_json_key_and_markdown_tamper_fail(self) -> None:
        self._collect()
        receipt_path = self.output / "metadata-freshness.json"
        original_receipt = receipt_path.read_bytes()
        receipt_path.write_text('{"schemaVersion":1,"schemaVersion":1}\n')
        with self.assertRaisesRegex(MetadataFreshnessError, "strict JSON"):
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=self.expectation,
            )
        receipt_path.write_bytes(original_receipt)
        (self.output / "snapshot-b.md").write_text("# tampered\n")
        with self.assertRaisesRegex(MetadataFreshnessError, "Markdown differs"):
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=self.expectation,
            )

    def test_stale_freshness_fails_with_exact_code(self) -> None:
        _, clock = self._collect()
        with self.assertRaises(MetadataFreshnessError) as raised:
            verify_metadata_freshness_artifact(
                artifact_directory=self.output,
                qualification_directory=self.qualification,
                expectation=self.expectation,
                verified_at=clock.now() + timedelta(minutes=16),
            )
        self.assertEqual(raised.exception.code, "METADATA_FRESHNESS_EXPIRED")

    def test_wrong_run_head_or_qualification_binding_fails(self) -> None:
        self._collect()
        expectations = (
            FreshnessExpectation(
                FRESHNESS_RUN_ID, "2" * 40, TREE, QUALIFICATION_RUN_ID, QUALIFICATION_ARTIFACT_ID
            ),
            FreshnessExpectation(
                FRESHNESS_RUN_ID, CANDIDATE, TREE, 999, QUALIFICATION_ARTIFACT_ID
            ),
        )
        for expectation in expectations:
            with self.subTest(expectation=expectation), self.assertRaises(
                MetadataFreshnessError
            ):
                verify_metadata_freshness_artifact(
                    artifact_directory=self.output,
                    qualification_directory=self.qualification,
                    expectation=expectation,
                )

    def test_exact_zip_extract_and_extra_or_missing_file_rejection(self) -> None:
        self._collect()
        archive = self.root / "freshness.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for path in self.output.iterdir():
                output.write(path, arcname=path.name)
        digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
        extracted = self.root / "extracted"
        self.assertEqual(
            extract_metadata_freshness_artifact(
                archive, extracted, expected_sha256=digest
            )["fileCount"],
            9,
        )
        for excluded in ("snapshot-a.md", None):
            with self.subTest(excluded=excluded):
                broken = self.root / f"broken-{excluded or 'extra'}.zip"
                with zipfile.ZipFile(broken, "w") as output:
                    for path in self.output.iterdir():
                        if path.name != excluded:
                            output.write(path, arcname=path.name)
                    if excluded is None:
                        output.writestr("extra.txt", "no")
                broken_digest = "sha256:" + hashlib.sha256(broken.read_bytes()).hexdigest()
                with self.assertRaises(MetadataFreshnessError):
                    extract_metadata_freshness_artifact(
                        broken,
                        self.root / f"extract-{excluded or 'extra'}",
                        expected_sha256=broken_digest,
                    )


class GitHubAdapterTests(unittest.TestCase):
    def test_http_diagnostics_are_closed_and_do_not_contain_token(self) -> None:
        class Response:
            def __init__(self) -> None:
                self.status = 200
                self.headers = {
                    "Content-Type": "application/json",
                    "X-GitHub-Request-Id": "request-id",
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": "4999",
                    "X-RateLimit-Used": "1",
                    "X-RateLimit-Reset": "123456",
                }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _maximum: int) -> bytes:
                return _json_bytes([_pull()])

        source = GitHubAssociatedPullSource(
            "github_pat_secret_material", opener=lambda *_args, **_kwargs: Response()
        )
        response = source.fetch(
            repository="yanyuhanyue/AniMemo",
            commit=COMMIT_ONE,
            snapshot="A",
            attempt=1,
        )
        serialized = json.dumps(response.diagnostic)
        self.assertEqual(set(response.diagnostic), DIAGNOSTIC_FIELDS)
        self.assertNotIn("github_pat_secret_material", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertEqual(response.diagnostic["githubRequestId"], "request-id")

    def test_http_error_classes_do_not_expose_headers_or_body(self) -> None:
        def opener(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://api.github.com/fixed",
                429,
                "rate limited",
                {"Retry-After": "7", "X-GitHub-Request-Id": "request-id"},
                io.BytesIO(b'{"message":"secondary rate limit"}'),
            )

        response = GitHubAssociatedPullSource("secret", opener=opener).fetch(
            repository="yanyuhanyue/AniMemo",
            commit=COMMIT_ONE,
            snapshot="A",
            attempt=1,
        )
        self.assertEqual(response.error_code, "SECONDARY_RATE_LIMIT")
        self.assertEqual(response.retry_after_seconds, 7)
        self.assertNotIn("secret", json.dumps(response.diagnostic))


if __name__ == "__main__":
    unittest.main()
