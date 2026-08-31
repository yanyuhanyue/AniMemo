from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from scripts.release_publication_controller import (
    ControllerReleaseAuthorityError,
    ControllerReleaseAuthorityVerifier,
    ProductionReleaseAuthorityObserver,
    ReleaseAuthorityEvidence,
    _GitHubReadOnlyObservationBoundary,
    _hash_file,
    _json_file,
    _verify_checksums,
    verify_controller_release_authority,
)

HEAD = "1" * 40
TREE = "2" * 40
CANDIDATE_INPUT = "sha256:" + "3" * 64
VERIFIED_CANDIDATE = "sha256:" + "4" * 64
CANDIDATE_RECEIPT = "sha256:" + "5" * 64
PUBLICATION = "sha256:" + "6" * 64
API_DIGEST = "sha256:" + "7" * 64
WEB_DIGEST = "sha256:" + "8" * 64
TAG = "v1.1.0-rc.19"
VERSION = "1.1.0-rc.19"

PROHIBITED_ACTIONS = (
    "PUBLIC_DNS_MUTATION",
    "CLOUDFLARE_CONTROL_PLANE",
    "FIREWALL_MUTATION",
    "OPENRESTY_MUTATION",
    "V1_0_STOP_DELETE_OR_REPLACE",
    "STABLE_PROMOTION_OR_TAG",
)


def authority_request() -> dict[str, object]:
    return {
        "schema": "animemo.rc19-release-stage-authority-request/v1",
        "finalRepoHead": HEAD,
        "finalRepoTree": TREE,
        "qualificationRunId": 101,
        "candidateInputSha256": CANDIDATE_INPUT,
        "verifiedCandidateIdentity": VERIFIED_CANDIDATE,
        "candidateAggregateReceiptSha256": CANDIDATE_RECEIPT,
        "releaseTag": TAG,
        "releaseVersion": VERSION,
        "releaseChannel": "rc",
        "publishRunId": 202,
        "mirrorRunId": 303,
        "publicationIdentity": PUBLICATION,
        "apiDigest": API_DIGEST,
        "webDigest": WEB_DIGEST,
        "publishRebuildCount": 0,
        "globalMutationFreeze": False,
    }


def public_identity() -> dict[str, object]:
    return {
        "schema": "animemo.rc19-release-public-identity/v1",
        "finalRepoHead": HEAD,
        "finalRepoTree": TREE,
        "qualificationRunId": 101,
        "qualificationRunAttempt": 1,
        "qualificationArtifacts": {
            "platformArtifactId": 11,
            "platformArtifactSha256": "sha256:" + "a" * 64,
            "dryRunArtifactId": 12,
            "dryRunArtifactSha256": "sha256:" + "b" * 64,
        },
        "candidateInputSha256": CANDIDATE_INPUT,
        "verifiedCandidateIdentity": VERIFIED_CANDIDATE,
        "releaseTag": TAG,
        "releaseVersion": VERSION,
        "releaseChannel": "rc",
        "continuationRoot": "C:/controller/continuation",
        "evidenceRoot": "C:/controller/evidence",
        "sealRoot": "C:/controller/seal",
        "releaseStageRoot": "C:/controller/release",
        "producerAuthority": {
            "schema": "animemo.remote-producer-authority-binding/v1",
            "archivePath": "C:/controller/producer.zip",
            "archiveSha256": "sha256:" + "c" * 64,
            "sealPath": "C:/controller/producer.json",
            "sealSha256": "sha256:" + "d" * 64,
            "producerArtifactId": 13,
            "producerArtifactName": "controller-authority-101",
        },
    }


def release_evidence() -> ReleaseAuthorityEvidence:
    return ReleaseAuthorityEvidence(
        final_repo_head=HEAD,
        final_repo_tree=TREE,
        qualification_run_id=101,
        candidate_input_sha256=CANDIDATE_INPUT,
        verified_candidate_identity=VERIFIED_CANDIDATE,
        candidate_aggregate_receipt_sha256=CANDIDATE_RECEIPT,
        release_tag=TAG,
        release_version=VERSION,
        release_channel="rc",
        publish_run_id=202,
        mirror_run_id=303,
        publication_identity=PUBLICATION,
        api_digest=API_DIGEST,
        web_digest=WEB_DIGEST,
        publish_rebuild_count=0,
        global_mutation_freeze=False,
        publish_result="PASS",
        mirror_result="PASS",
        remote_readback_result="PASS",
        zero_rebuild=True,
    )


class StaticObserver:
    def __init__(self, evidence: ReleaseAuthorityEvidence) -> None:
        self.evidence = evidence

    def observe(
        self,
        *,
        authority_request: dict[str, object],
        expected_public_identity: dict[str, object],
        candidate_result: dict[str, object],
    ) -> ReleaseAuthorityEvidence:
        del authority_request, expected_public_identity, candidate_result
        return self.evidence


class _HTTPResponse:
    def __init__(
        self,
        *,
        url: str,
        status: int = 200,
        headers: Message | None = None,
        body: bytes = b"",
    ) -> None:
        self._url = url
        self.status = status
        self.headers = headers if headers is not None else Message()
        self._body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self._body.close()


class _AnonymousGhcrOpener:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []

    def open(self, request, *, timeout: int):
        url = request.full_url
        authorization = request.get_header("Authorization")
        self.requests.append((request.method, url, authorization))
        if url.startswith("https://ghcr.io/v2/") and authorization is None:
            repository = url.removeprefix("https://ghcr.io/v2/").split(
                "/manifests/", 1
            )[0]
            headers = Message()
            headers["WWW-Authenticate"] = (
                'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                f'scope="repository:{repository}:pull"'
            )
            raise HTTPError(url, 401, "Unauthorized", headers, None)
        if url.startswith("https://ghcr.io/token?"):
            return _HTTPResponse(
                url=url,
                body=json.dumps({"token": "fixed-anonymous-token"}).encode("ascii"),
            )
        if url.startswith("https://ghcr.io/v2/"):
            headers = Message()
            digest = API_DIGEST if "/animemo-api/" in url else WEB_DIGEST
            headers["Docker-Content-Digest"] = digest
            return _HTTPResponse(url=url, headers=headers)
        raise AssertionError(f"unexpected URL: {url}")


def _tool_authority(root: Path) -> dict[str, str]:
    executable = root / "gh-test.exe"
    payload = b"fixed-gh-tool-authority"
    executable.write_bytes(payload)
    return {
        "gh_executable": str(executable),
        "gh_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


class ControllerReleaseAuthorityVerifierTests(unittest.TestCase):
    def test_controller_entrypoint_has_the_fixed_four_keyword_contract(self):
        self.assertEqual(
            tuple(inspect.signature(verify_controller_release_authority).parameters),
            (
                "authority_request",
                "expected_public_identity",
                "candidate_result",
                "prohibited_actions",
            ),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in inspect.signature(
                    verify_controller_release_authority
                ).parameters.values()
            )
        )

    def test_exact_read_only_evidence_returns_closed_controller_observation(self):
        request = authority_request()
        evidence = release_evidence()
        verifier = ControllerReleaseAuthorityVerifier(StaticObserver(evidence))

        result = verifier.verify_controller_release_authority(
            authority_request=request,
            expected_public_identity=public_identity(),
            candidate_result={
                "status": "PASS",
                "candidateAggregateReceiptDigest": CANDIDATE_RECEIPT,
            },
            prohibited_actions=PROHIBITED_ACTIONS,
        )

        self.assertEqual(
            result,
            {
                **{key: value for key, value in request.items() if key != "schema"},
                "schema": "animemo.rc19-release-authority-observation/v1",
                "result": "PASS",
                "releaseAuthorityResult": "PASS",
                "mirrorResult": "PASS",
                "remoteReadbackResult": "PASS",
                "zeroRebuild": True,
            },
        )

    def test_production_subprocess_boundary_is_read_only_and_has_no_crane_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            authority = _tool_authority(Path(temporary))
            boundary = _GitHubReadOnlyObservationBoundary(**authority)
            completed = subprocess.CompletedProcess(
                args=(authority["gh_executable"], "api"),
                returncode=0,
                stdout=b"{}",
                stderr=b"",
            )
            with mock.patch(
                "scripts.release_publication_controller.subprocess.run",
                return_value=completed,
            ) as run:
                observed = boundary._run(
                    (
                        "gh",
                        "api",
                        "--method",
                        "GET",
                        f"repos/yanyuhanyue/AniMemo/git/commits/{HEAD}",
                    )
                )

            self.assertEqual(observed, b"{}")
            self.assertEqual(
                run.call_args.args[0][0:4],
                ("gh.exe", "api", "--method", "GET"),
            )
            self.assertEqual(
                run.call_args.kwargs["executable"], authority["gh_executable"]
            )
            self.assertTrue(Path(run.call_args.kwargs["executable"]).is_absolute())
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(run.call_args.kwargs["shell"])
            with self.assertRaisesRegex(
                ControllerReleaseAuthorityError,
                "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
            ):
                boundary._run(
                    ("crane", "digest", f"ghcr.io/yanyuhanyue/animemo-api:{TAG}")
                )
            for command in (
                ("gh", "api", "--method", "POST", "repos/yanyuhanyue/AniMemo"),
                ("gh", "run", "cancel", "202", "--repo", "yanyuhanyue/AniMemo"),
                (
                    "gh",
                    "release",
                    "delete",
                    TAG,
                    "--repo",
                    "yanyuhanyue/AniMemo",
                ),
            ):
                with (
                    self.subTest(command=command),
                    self.assertRaisesRegex(
                        ControllerReleaseAuthorityError,
                        "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
                    ),
                ):
                    boundary._run(command)

    def test_production_boundary_has_no_path_fallback_for_gh(self):
        with (
            mock.patch.dict(
                os.environ,
                {"ANIMEMO_GH_EXECUTABLE": "", "ANIMEMO_GH_SHA256": ""},
                clear=False,
            ),
            self.assertRaisesRegex(
                ControllerReleaseAuthorityError,
                "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE$",
            ),
        ):
            _GitHubReadOnlyObservationBoundary()

    def test_production_boundary_rejects_non_executable_script_tool_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "gh-test.cmd"
            payload = b"@echo off\r\n"
            script.write_bytes(payload)
            with self.assertRaisesRegex(
                ControllerReleaseAuthorityError,
                "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE$",
            ):
                _GitHubReadOnlyObservationBoundary(
                    gh_executable=str(script),
                    gh_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
                )

    def test_gh_tool_digest_mismatch_fails_before_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            authority = _tool_authority(Path(temporary))
            authority["gh_sha256"] = "sha256:" + "0" * 64
            with (
                mock.patch(
                    "scripts.release_publication_controller.subprocess.run",
                    side_effect=AssertionError("untrusted tool must not execute"),
                ),
                self.assertRaisesRegex(
                    ControllerReleaseAuthorityError,
                    "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE$",
                ),
            ):
                _GitHubReadOnlyObservationBoundary(**authority)

    def test_registry_digest_uses_fixed_anonymous_ghcr_bearer_flow(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        opener = _AnonymousGhcrOpener()
        boundary = _GitHubReadOnlyObservationBoundary(
            **_tool_authority(Path(temporary.name)), opener=opener
        )

        with mock.patch(
            "scripts.release_publication_controller.subprocess.run",
            side_effect=AssertionError("registry verification must not launch a tool"),
        ):
            boundary._verify_registry(authority_request())

        manifest_requests = [
            item
            for item in opener.requests
            if item[1].startswith("https://ghcr.io/v2/")
        ]
        self.assertEqual(len(manifest_requests), 8)
        self.assertEqual(
            {item[0] for item in manifest_requests},
            {"HEAD"},
        )
        self.assertEqual(
            {item[1] for item in manifest_requests},
            {
                f"https://ghcr.io/v2/yanyuhanyue/animemo-api/manifests/{TAG}",
                f"https://ghcr.io/v2/yanyuhanyue/animemo-api/manifests/sha-{HEAD}",
                f"https://ghcr.io/v2/yanyuhanyue/animemo-web/manifests/{TAG}",
                f"https://ghcr.io/v2/yanyuhanyue/animemo-web/manifests/sha-{HEAD}",
            },
        )
        self.assertEqual(
            {item[2] for item in manifest_requests if item[2] is not None},
            {"Bearer fixed-anonymous-token"},
        )
        token_requests = [
            item
            for item in opener.requests
            if item[1].startswith("https://ghcr.io/token?")
        ]
        self.assertEqual(len(token_requests), 4)
        self.assertTrue(
            all(
                item[0] == "GET" and "service=ghcr.io" in item[1] and item[2] is None
                for item in token_requests
            )
        )

    def test_registry_digest_rejects_non_ghcr_bearer_authority(self):
        class WrongAuthorityOpener:
            def open(self, request, *, timeout: int):
                del timeout
                headers = Message()
                headers["WWW-Authenticate"] = (
                    'Bearer realm="https://example.invalid/token",'
                    'service="ghcr.io",'
                    'scope="repository:yanyuhanyue/animemo-api:pull"'
                )
                raise HTTPError(request.full_url, 401, "Unauthorized", headers, None)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        boundary = _GitHubReadOnlyObservationBoundary(
            **_tool_authority(Path(temporary.name)), opener=WrongAuthorityOpener()
        )

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
        ):
            boundary._verify_registry(authority_request())

    def test_registry_digest_rejects_redirected_token_response(self):
        class RedirectedTokenOpener(_AnonymousGhcrOpener):
            def open(self, request, *, timeout: int):
                if request.full_url.startswith("https://ghcr.io/token?"):
                    return _HTTPResponse(
                        url="https://example.invalid/token",
                        body=b'{"token":"fixed-anonymous-token"}',
                    )
                return super().open(request, timeout=timeout)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        boundary = _GitHubReadOnlyObservationBoundary(
            **_tool_authority(Path(temporary.name)), opener=RedirectedTokenOpener()
        )

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE$",
        ):
            boundary._verify_registry(authority_request())

    def test_registry_digest_rejects_tag_path_characters_before_http(self):
        class ForbiddenOpener:
            def open(self, request, *, timeout: int):
                raise AssertionError((request, timeout))

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        boundary = _GitHubReadOnlyObservationBoundary(
            **_tool_authority(Path(temporary.name)), opener=ForbiddenOpener()
        )

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
        ):
            boundary._ghcr_digest(role="api", tag="../latest")

    def test_json_reader_rejects_path_replacement_between_lstat_and_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "authority.json"
            replacement = root / "replacement.json"
            target.write_bytes(b'{"value":1}')
            replacement.write_bytes(b'{"value":2}')
            original_open = Path.open
            replaced = False

            def replacing_open(path, *args, **kwargs):
                nonlocal replaced
                if path == target and not replaced:
                    replaced = True
                    os.replace(replacement, target)
                return original_open(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", new=replacing_open),
                self.assertRaisesRegex(
                    ControllerReleaseAuthorityError,
                    "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
                ),
            ):
                _json_file(target)

    def test_hash_reader_rejects_growth_after_the_handle_is_opened(self):
        class GrowingReader:
            def __init__(self, stream, path: Path) -> None:
                self._stream = stream
                self._path = path
                self._grown = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self._stream.close()

            def fileno(self):
                return self._stream.fileno()

            def close(self):
                self._stream.close()

            def read(self, size=-1):
                if not self._grown:
                    self._grown = True
                    descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND)
                    try:
                        os.write(descriptor, b"grown")
                    finally:
                        os.close(descriptor)
                return self._stream.read(size)

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "asset.bin"
            target.write_bytes(b"fixed")
            original_open = Path.open

            def growing_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                return GrowingReader(stream, path) if path == target else stream

            with (
                mock.patch.object(Path, "open", new=growing_open),
                self.assertRaisesRegex(
                    ControllerReleaseAuthorityError,
                    "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
                ),
            ):
                _hash_file(target, maximum=1024)

    def test_checksums_reader_rejects_path_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "release-manifest.json": b"manifest",
                "deployment-contract.json": b"contract",
                "installer-materials.tar": b"installer",
            }
            for name, value in files.items():
                (root / name).write_bytes(value)
            checksums = root / "checksums.txt"
            replacement = root / "replacement.txt"
            lines = "".join(
                f"{__import__('hashlib').sha256(value).hexdigest()}  {name}\n"
                for name, value in files.items()
            ).encode("ascii")
            checksums.write_bytes(lines)
            replacement.write_bytes(lines)
            original_open = Path.open
            replaced = False

            def replacing_open(path, *args, **kwargs):
                nonlocal replaced
                if path == checksums and not replaced:
                    replaced = True
                    os.replace(replacement, checksums)
                return original_open(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", new=replacing_open),
                self.assertRaisesRegex(
                    ControllerReleaseAuthorityError,
                    "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
                ),
            ):
                _verify_checksums(root)

    def test_signed_public_identity_mismatch_fails_before_observation(self):
        class ForbiddenObserver:
            def observe(self, **_):
                raise AssertionError("observer must not run for an unbound request")

        identity = public_identity()
        identity["finalRepoTree"] = "9" * 40
        verifier = ControllerReleaseAuthorityVerifier(ForbiddenObserver())

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_INPUT_INVALID$",
        ):
            verifier.verify_controller_release_authority(
                authority_request=authority_request(),
                expected_public_identity=identity,
                candidate_result={
                    "status": "PASS",
                    "candidateAggregateReceiptDigest": CANDIDATE_RECEIPT,
                },
                prohibited_actions=PROHIBITED_ACTIONS,
            )

    def test_candidate_receipt_mismatch_fails_before_observation(self):
        class ForbiddenObserver:
            def observe(self, **_):
                raise AssertionError("observer must not run for an unbound request")

        verifier = ControllerReleaseAuthorityVerifier(ForbiddenObserver())

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_INPUT_INVALID$",
        ):
            verifier.verify_controller_release_authority(
                authority_request=authority_request(),
                expected_public_identity=public_identity(),
                candidate_result={
                    "status": "PASS",
                    "candidateAggregateReceiptDigest": "sha256:" + "0" * 64,
                },
                prohibited_actions=PROHIBITED_ACTIONS,
            )

    def test_incomplete_observation_cannot_produce_a_pass(self):
        verifier = ControllerReleaseAuthorityVerifier(
            StaticObserver(replace(release_evidence(), remote_readback_result="FAILED"))
        )

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
        ):
            verifier.verify_controller_release_authority(
                authority_request=authority_request(),
                expected_public_identity=public_identity(),
                candidate_result={
                    "status": "PASS",
                    "candidateAggregateReceiptDigest": CANDIDATE_RECEIPT,
                },
                prohibited_actions=PROHIBITED_ACTIONS,
            )

    def test_production_boundary_cancellation_is_stably_fail_closed(self):
        class InterruptedBoundary:
            def observe(self, **_):
                raise KeyboardInterrupt

        observer = ProductionReleaseAuthorityObserver(InterruptedBoundary())

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE$",
        ) as raised:
            observer.observe(
                authority_request=authority_request(),
                expected_public_identity=public_identity(),
                candidate_result={
                    "status": "PASS",
                    "candidateAggregateReceiptDigest": CANDIDATE_RECEIPT,
                },
            )
        self.assertIsNone(raised.exception.__cause__)

    def test_production_boundary_permanent_domain_errors_are_not_retryable(self):
        class InvalidBoundary:
            def __init__(self, error):
                self._error = error

            def observe(self, **_):
                raise self._error

        for error in (ValueError("invalid candidate"), RuntimeError("invalid ledger")):
            with self.subTest(error=type(error).__name__):
                observer = ProductionReleaseAuthorityObserver(InvalidBoundary(error))
                with self.assertRaisesRegex(
                    ControllerReleaseAuthorityError,
                    "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_INVALID$",
                ) as raised:
                    observer.observe(
                        authority_request=authority_request(),
                        expected_public_identity=public_identity(),
                        candidate_result={
                            "status": "PASS",
                            "candidateAggregateReceiptDigest": CANDIDATE_RECEIPT,
                        },
                    )
                self.assertIsNone(raised.exception.__cause__)

    def test_production_boundary_transient_operating_error_is_retryable(self):
        class UnavailableBoundary:
            def observe(self, **_):
                raise OSError("temporary read failure")

        observer = ProductionReleaseAuthorityObserver(UnavailableBoundary())
        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_EVIDENCE_UNAVAILABLE$",
        ) as raised:
            observer.observe(
                authority_request=authority_request(),
                expected_public_identity=public_identity(),
                candidate_result={
                    "status": "PASS",
                    "candidateAggregateReceiptDigest": CANDIDATE_RECEIPT,
                },
            )
        self.assertIsNone(raised.exception.__cause__)

    def test_stable_authority_is_rejected_before_any_external_observation(self):
        class ForbiddenObserver:
            def observe(self, **_):
                raise AssertionError("Stable must never reach the observer")

        request = authority_request()
        request.update(
            releaseTag="v1.1.0",
            releaseVersion="1.1.0",
            releaseChannel="stable",
        )
        identity = public_identity()
        identity.update(
            releaseTag="v1.1.0",
            releaseVersion="1.1.0",
            releaseChannel="stable",
        )
        verifier = ControllerReleaseAuthorityVerifier(ForbiddenObserver())

        with self.assertRaisesRegex(
            ControllerReleaseAuthorityError,
            "^CONTROLLER_RELEASE_AUTHORITY_INPUT_INVALID$",
        ):
            verifier.verify_controller_release_authority(
                authority_request=request,
                expected_public_identity=identity,
                candidate_result={
                    "status": "PASS",
                    "candidateAggregateReceiptDigest": CANDIDATE_RECEIPT,
                },
                prohibited_actions=PROHIBITED_ACTIONS,
            )


if __name__ == "__main__":
    unittest.main()
