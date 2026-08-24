from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import updater.transport as transport_module
from updater.errors import CommandExited, CommandTimedOut
from updater.transport import (
    DEFAULT_TRANSFER_BUDGET_POLICY,
    RELEASE_BUNDLE_OBJECTS,
    ExplicitTransportPolicy,
    GitHubTransportSource,
    OfficialMirrorTransportSource,
    TransportError,
    TransportObjectPlan,
    TransportRequest,
    TransportSourceId,
)

RC10_CAPTURED_TIMEOUT_FIXTURE = {
    "version": "v1.1.0-rc.10",
    "repository": "yanyuhanyue/AniMemo",
    "objects": RELEASE_BUNDLE_OBJECTS,
    "installer_materials_size": 62_484_480,
    "timeout_seconds": 60,
    "attempt_modes": ("anonymous", "authenticated"),
    "exception_types": (
        "TransportError",
        "CommandFailed",
        "TimeoutExpired",
        "CommandFailed",
        "TimeoutExpired",
    ),
    "exception_messages": (
        "GitHub transport is unavailable",
        "command failed: /usr/bin/gh; stdout=; stderr=",
        (
            "Command ['/usr/bin/gh', 'release', 'download', 'v1.1.0-rc.10', "
            "'--repo', 'yanyuhanyue/AniMemo', '--pattern', 'checksums.txt', "
            "'--pattern', 'deployment-contract.json', '--pattern', "
            "'installer-materials.tar', '--pattern', 'release-manifest.json', "
            "'--dir', '/tmp/rc10-release-diagnostic-cache/.v1.1.0-rc.10.35hso9f3/"
            ".transport-pending-bpqbtm9c'] timed out after 60 seconds"
        ),
        "command failed: /usr/bin/gh; stdout=; stderr=",
        (
            "Command ['/usr/bin/gh', 'release', 'download', 'v1.1.0-rc.10', "
            "'--repo', 'yanyuhanyue/AniMemo', '--pattern', 'checksums.txt', "
            "'--pattern', 'deployment-contract.json', '--pattern', "
            "'installer-materials.tar', '--pattern', 'release-manifest.json', "
            "'--dir', '/tmp/rc10-release-diagnostic-cache/.v1.1.0-rc.10.35hso9f3/"
            ".transport-pending-bpqbtm9c'] timed out after 60 seconds"
        ),
    ),
    "legacy_final_code": "TRANSPORT_UNAVAILABLE",
}


class FakeResponse:
    def __init__(self, body: bytes, url: str, *, declared_length: int | None = None):
        self._stream = io.BytesIO(body)
        self._url = url
        self.headers = {
            "Content-Length": str(
                len(body) if declared_length is None else declared_length
            )
        }

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stream.close()


class FakeOpener:
    def __init__(
        self,
        objects: dict[str, bytes],
        *,
        declared_lengths: dict[str, int] | None = None,
        redirected_urls: dict[str, str] | None = None,
        errors: dict[str, BaseException] | None = None,
    ):
        self.objects = objects
        self.declared_lengths = declared_lengths or {}
        self.redirected_urls = redirected_urls or {}
        self.errors = errors or {}
        self.urls: list[str] = []

    def open(self, request, timeout: int):
        del timeout
        url = request.full_url
        self.urls.append(url)
        name = Path(urlsplit(url).path).name
        if name in self.errors:
            raise self.errors[name]
        return FakeResponse(
            self.objects[name],
            self.redirected_urls.get(name, url),
            declared_length=self.declared_lengths.get(name),
        )


def link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )


def object_plans(objects: dict[str, bytes]) -> tuple[TransportObjectPlan, ...]:
    return tuple(
        TransportObjectPlan(name, len(objects[name])) for name in RELEASE_BUNDLE_OBJECTS
    )


class RecordingGitHubRunner:
    def __init__(
        self,
        bodies: dict[str, bytes],
        outcomes=None,
        *,
        extra_file=False,
        unsafe_kind=None,
    ):
        self.bodies = bodies
        self.outcomes = {
            name: list(values) for name, values in (outcomes or {}).items()
        }
        self.extra_file = extra_file
        self.unsafe_kind = unsafe_kind
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(self, argv, **kwargs):
        logical_name = argv[argv.index("--pattern") + 1]
        self.calls.append((tuple(argv), kwargs))
        outcomes = self.outcomes.get(logical_name, [])
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, tuple) and outcome[0] == "partial":
                destination = Path(argv[argv.index("--dir") + 1])
                destination.mkdir(parents=True, exist_ok=True)
                (destination / logical_name).write_bytes(b"partial-attempt")
                raise outcome[1]
            if isinstance(outcome, BaseException):
                raise outcome
        destination = Path(argv[argv.index("--dir") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / logical_name
        if self.unsafe_kind == "directory":
            target.mkdir()
        elif self.unsafe_kind == "hardlink":
            outside = destination.parent / ".hardlink-source"
            outside.write_bytes(self.bodies[logical_name])
            target.hardlink_to(outside)
        elif self.unsafe_kind == "symlink":
            outside = destination.parent / ".symlink-source"
            outside.write_bytes(self.bodies[logical_name])
            try:
                target.symlink_to(outside)
            except OSError as error:
                raise unittest.SkipTest(f"symlinks are unavailable: {error}") from error
        elif self.unsafe_kind == "missing":
            pass
        else:
            target.write_bytes(self.bodies[logical_name])
        if self.extra_file:
            (destination / "unexpected.txt").write_text("unexpected", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeMonotonic:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += seconds


class SlowGitHubRunner(RecordingGitHubRunner):
    def __init__(self, bodies, clock, required_seconds):
        super().__init__(bodies)
        self.clock = clock
        self.required_seconds = required_seconds

    def run(self, argv, **kwargs):
        logical_name = argv[argv.index("--pattern") + 1]
        required = self.required_seconds.get(logical_name, 0)
        timeout = kwargs["timeout"]
        self.clock.advance(min(required, timeout))
        if timeout < required:
            raise CommandTimedOut("/usr/bin/gh", timeout, "", "")
        if logical_name == "installer-materials.tar":
            self.calls.append((tuple(argv), kwargs))
            destination = Path(argv[argv.index("--dir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            with (destination / logical_name).open("wb") as output:
                output.seek(62_484_480 - 1)
                output.write(b"x")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return super().run(argv, **kwargs)


class AdvancingTimeoutRunner(RecordingGitHubRunner):
    def __init__(self, bodies, clock, advance_seconds):
        super().__init__(bodies)
        self.clock = clock
        self.advance_seconds = advance_seconds

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        self.clock.advance(self.advance_seconds)
        raise CommandTimedOut("/usr/bin/gh", kwargs["timeout"], "", "")


class Rc10CapturedFailureRunner(RecordingGitHubRunner):
    def __init__(self, bodies):
        super().__init__(bodies)
        self.installer_attempts = 0

    def run(self, argv, **kwargs):
        logical_name = argv[argv.index("--pattern") + 1]
        if logical_name != "installer-materials.tar":
            return super().run(argv, **kwargs)
        self.calls.append((tuple(argv), kwargs))
        self.installer_attempts += 1
        if self.installer_attempts <= 2:
            raise CommandTimedOut("/usr/bin/gh", kwargs["timeout"], "", "")
        destination = Path(argv[argv.index("--dir") + 1])
        destination.mkdir(parents=True, exist_ok=True)
        with (destination / logical_name).open("wb") as output:
            output.seek(62_484_480 - 1)
            output.write(b"x")
        return subprocess.CompletedProcess(argv, 0, "", "")


class ExplicitTransportPolicyTests(unittest.TestCase):
    def test_policy_accepts_only_explicit_closed_transport_sources(self):
        github = ExplicitTransportPolicy.github()
        mirror = ExplicitTransportPolicy.official_mirror()

        self.assertEqual(github.source, TransportSourceId.GITHUB)
        self.assertEqual(mirror.source, TransportSourceId.OFFICIAL_MIRROR)
        self.assertFalse(github.fallback_allowed)
        self.assertFalse(mirror.fallback_allowed)
        self.assertRegex(github.identity, r"^[0-9a-f]{64}$")
        self.assertNotEqual(github.identity, mirror.identity)

        with self.assertRaises(TransportError) as raised:
            ExplicitTransportPolicy(source="auto")
        self.assertEqual(raised.exception.code, "TRANSPORT_POLICY_INVALID")


class TransportRequestTests(unittest.TestCase):
    def test_release_request_has_a_fixed_object_set_and_no_url_input(self):
        plans = tuple(TransportObjectPlan(name, 1) for name in RELEASE_BUNDLE_OBJECTS)
        request = TransportRequest.release_bundle("1.1.0-rc.2", object_plans=plans)

        self.assertEqual(request.exact_version, "1.1.0-rc.2")
        self.assertEqual(request.objects, RELEASE_BUNDLE_OBJECTS)
        self.assertRegex(request.identity, r"^[0-9a-f]{64}$")

        for invalid in ("v1.1.0", "../1.1.0", "1.1", "1.1.0/asset"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TransportError) as raised:
                    TransportRequest.release_bundle(invalid, object_plans=plans)
                self.assertEqual(
                    raised.exception.code,
                    "TRANSPORT_REQUEST_INVALID",
                )

        with self.assertRaises(TypeError):
            TransportRequest.release_bundle(
                "1.1.0",
                object_plans=plans,
                url="https://attacker.invalid/release",
            )


class TransportSourceTests(unittest.TestCase):
    def test_github_and_mirror_acquire_the_same_fixed_transport_objects(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        request = TransportRequest.release_bundle(
            "1.1.0", object_plans=object_plans(bodies)
        )
        github_runner = RecordingGitHubRunner(bodies)
        mirror_opener = FakeOpener(bodies)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            github_staging = root / "github"
            mirror_staging = root / "mirror"
            github_staging.mkdir(mode=0o700)
            mirror_staging.mkdir(mode=0o700)

            github = GitHubTransportSource(runner=github_runner).acquire(
                request,
                github_staging,
            )
            mirror = OfficialMirrorTransportSource(opener=mirror_opener).acquire(
                request,
                mirror_staging,
            )

            self.assertEqual(github.receipt.transport_id, TransportSourceId.GITHUB)
            self.assertEqual(
                mirror.receipt.transport_id,
                TransportSourceId.OFFICIAL_MIRROR,
            )
            self.assertEqual(
                [
                    (item.logical_name, item.sha256, item.size)
                    for item in github.objects
                ],
                [
                    (item.logical_name, item.sha256, item.size)
                    for item in mirror.objects
                ],
            )
            for name, body in bodies.items():
                self.assertEqual(github.material(name).read_bytes(), body)
                self.assertEqual(mirror.material(name).read_bytes(), body)
            self.assertRegex(github.receipt.identity, r"^[0-9a-f]{64}$")
            self.assertRegex(mirror.receipt.identity, r"^[0-9a-f]{64}$")
            self.assertFalse(hasattr(github, "manifest"))
            self.assertFalse(hasattr(mirror, "manifest"))

        self.assertEqual(len(github_runner.calls), 4)
        self.assertTrue(
            all(
                call[0][call[0].index("--repo") + 1] == "yanyuhanyue/AniMemo"
                for call in github_runner.calls
            )
        )
        self.assertEqual(
            {urlsplit(url).netloc for url in mirror_opener.urls},
            {"download.animemo.app"},
        )

    def test_oversized_object_fails_closed_and_removes_partial_state(self):
        bodies = {name: (b"x" * 4) for name in RELEASE_BUNDLE_OBJECTS}
        bodies["installer-materials.tar"] = b"x" * 9
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                TransportRequest.release_bundle(
                    "1.1.0",
                    object_plans=object_plans(bodies),
                    max_object_bytes=8,
                    max_total_bytes=32,
                )

            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_REQUEST_INVALID",
            )
            self.assertEqual(list(staging.iterdir()), [])

    def test_aggregate_limit_can_be_stricter_than_the_per_object_limit(self):
        bodies = {name: b"xx" for name in RELEASE_BUNDLE_OBJECTS}
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                TransportRequest.release_bundle(
                    "1.1.0",
                    object_plans=object_plans(bodies),
                    max_object_bytes=8,
                    max_total_bytes=6,
                )
            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_REQUEST_INVALID",
            )
            self.assertEqual(list(staging.iterdir()), [])

    def test_declared_length_mismatch_is_rejected_and_not_committed(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        opener = FakeOpener(
            bodies,
            declared_lengths={"checksums.txt": len(bodies["checksums.txt"]) + 1},
        )
        request = TransportRequest.release_bundle(
            "1.1.0", object_plans=object_plans(bodies)
        )

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                OfficialMirrorTransportSource(opener=opener).acquire(request, staging)

            self.assertEqual(raised.exception.code, "TRANSPORT_OBJECT_SIZE_MISMATCH")
            self.assertEqual(list(staging.iterdir()), [])

    def test_redirect_and_missing_object_have_stable_fail_closed_errors(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        request = TransportRequest.release_bundle(
            "1.1.0", object_plans=object_plans(bodies)
        )
        cases = (
            (
                FakeOpener(
                    bodies,
                    redirected_urls={
                        "checksums.txt": "https://attacker.invalid/checksums.txt"
                    },
                ),
                "TRANSPORT_REDIRECT_REJECTED",
            ),
            (
                FakeOpener(
                    bodies,
                    errors={
                        "checksums.txt": HTTPError(
                            "https://github.com/fixed",
                            404,
                            "missing",
                            None,
                            None,
                        )
                    },
                ),
                "TRANSPORT_OBJECT_MISSING",
            ),
        )

        for opener, expected_code in cases:
            with (
                self.subTest(expected_code=expected_code),
                tempfile.TemporaryDirectory() as temporary,
            ):
                staging = Path(temporary).resolve() / "private"
                staging.mkdir(mode=0o700)
                with self.assertRaises(TransportError) as raised:
                    OfficialMirrorTransportSource(opener=opener).acquire(
                        request,
                        staging,
                    )
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(list(staging.iterdir()), [])

    def test_interrupted_transaction_removes_every_partial_object(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        opener = FakeOpener(
            bodies,
            errors={"installer-materials.tar": URLError("fixture unavailable")},
        )

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                OfficialMirrorTransportSource(opener=opener).acquire(
                    TransportRequest.release_bundle(
                        "1.1.0", object_plans=object_plans(bodies)
                    ),
                    staging,
                )

            self.assertEqual(raised.exception.code, "TRANSPORT_UNAVAILABLE")
            self.assertTrue(raised.exception.retriable)
            self.assertEqual(list(staging.iterdir()), [])

    def test_timeout_has_a_stable_retriable_classification(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        opener = FakeOpener(
            bodies,
            errors={"checksums.txt": TimeoutError("fixture timeout")},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                OfficialMirrorTransportSource(opener=opener).acquire(
                    TransportRequest.release_bundle(
                        "1.1.0", object_plans=object_plans(bodies)
                    ),
                    staging,
                )
            self.assertEqual(raised.exception.code, "TRANSPORT_TIMEOUT")
            self.assertTrue(raised.exception.retriable)
            self.assertEqual(list(staging.iterdir()), [])

    def test_official_mirror_endpoint_and_policy_cannot_be_user_urls_or_auto(self):
        with self.assertRaises(TransportError) as raised:
            OfficialMirrorTransportSource(
                endpoint_id="geo-auto",
                opener=FakeOpener({}),
            )
        self.assertEqual(
            raised.exception.code,
            "TRANSPORT_SOURCE_UNSUPPORTED",
        )

        with self.assertRaises(TypeError):
            OfficialMirrorTransportSource(
                opener=FakeOpener({}),
                origin="https://attacker.invalid",
            )
        with self.assertRaises(TypeError):
            ExplicitTransportPolicy(
                source=TransportSourceId.GITHUB,
                fallback_allowed=True,
            )

    def test_acquired_material_rejects_hardlink_replacement(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            staging = base / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=RecordingGitHubRunner(bodies)
            ).acquire(
                TransportRequest.release_bundle(
                    "1.1.0", object_plans=object_plans(bodies)
                ),
                staging,
            )
            material = acquired.material("checksums.txt")
            external = base / "external-copy"
            external.write_bytes(material.read_bytes())
            material.unlink()
            try:
                material.hardlink_to(external)
            except OSError as error:
                self.skipTest(f"hardlinks are unavailable: {error}")

            with self.assertRaises(TransportError) as raised:
                acquired.material("checksums.txt")
            self.assertEqual(raised.exception.code, "TRANSPORT_PATH_UNSAFE")

    def test_acquired_material_rejects_byte_tampering_against_its_receipt(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = OfficialMirrorTransportSource(opener=FakeOpener(bodies)).acquire(
                TransportRequest.release_bundle(
                    "1.1.0", object_plans=object_plans(bodies)
                ),
                staging,
            )
            material = acquired.material("release-manifest.json")
            material.write_bytes(b"tampered transport bytes")

            with self.assertRaises(TransportError) as raised:
                acquired.material("release-manifest.json")
            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_RECEIPT_INVALID",
            )

    def test_staging_must_be_an_absolute_direct_private_directory(self):
        source = GitHubTransportSource(runner=RecordingGitHubRunner({}))
        with self.assertRaises(TransportError) as raised:
            source.acquire(
                TransportRequest.release_bundle(
                    "1.1.0",
                    object_plans=tuple(
                        TransportObjectPlan(name, 1) for name in RELEASE_BUNDLE_OBJECTS
                    ),
                ),
                Path("relative-staging"),
            )
        self.assertEqual(raised.exception.code, "TRANSPORT_PATH_UNSAFE")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            direct = base / "direct"
            link = base / "link"
            direct.mkdir(mode=0o700)
            link_directory(link, direct)
            with self.assertRaises(TransportError) as raised:
                source.acquire(
                    TransportRequest.release_bundle(
                        "1.1.0",
                        object_plans=tuple(
                            TransportObjectPlan(name, 1)
                            for name in RELEASE_BUNDLE_OBJECTS
                        ),
                    ),
                    link,
                )
            self.assertEqual(raised.exception.code, "TRANSPORT_PATH_UNSAFE")

    def test_transport_receipt_identity_excludes_random_staging_location(self):
        bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        request = TransportRequest.release_bundle(
            "1.1.0", object_plans=object_plans(bodies)
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            first_staging = base / "first"
            second_staging = base / "second"
            first_staging.mkdir(mode=0o700)
            second_staging.mkdir(mode=0o700)
            first = GitHubTransportSource(runner=RecordingGitHubRunner(bodies)).acquire(
                request,
                first_staging,
            )
            second = GitHubTransportSource(
                runner=RecordingGitHubRunner(bodies)
            ).acquire(
                request,
                second_staging,
            )

            self.assertNotEqual(first.root, second.root)
            self.assertEqual(first.receipt.identity, second.receipt.identity)


class ReleaseAssetBudgetAndObjectTransportTests(unittest.TestCase):
    def setUp(self):
        self.bodies = {
            name: f"fixed:{name}".encode("ascii") for name in RELEASE_BUNDLE_OBJECTS
        }
        self.request = TransportRequest.release_bundle(
            "1.1.0-rc.10",
            object_plans=object_plans(self.bodies),
        )

    def test_closed_budget_policy_has_the_exact_rc10_timeout_and_bounds(self):
        policy = DEFAULT_TRANSFER_BUDGET_POLICY

        self.assertEqual(policy.policy_version, 1)
        self.assertEqual(policy.minimum_object_timeout_seconds, 60)
        self.assertEqual(policy.object_timeout_base_seconds, 30)
        self.assertEqual(policy.minimum_expected_throughput_bytes_per_second, 131072)
        self.assertEqual(policy.maximum_object_timeout_seconds, 900)
        self.assertEqual(policy.maximum_attempts_per_object, 3)
        self.assertEqual(policy.backoff_seconds, (10, 30))
        self.assertEqual(policy.maximum_bundle_elapsed_seconds, 1800)
        self.assertEqual(policy.maximum_credential_transitions_per_object, 1)
        self.assertEqual(policy.timeout_for_size(62_484_480), 507)
        self.assertEqual(policy.timeout_for_size(1), 60)
        self.assertEqual(policy.timeout_for_size(1024 * 1024), 60)
        self.assertEqual(policy.timeout_for_size(64 * 1024 * 1024), 542)
        self.assertEqual(policy.timeout_for_size(512 * 1024 * 1024), 900)
        self.assertRegex(policy.identity, r"^sha256:[0-9a-f]{64}$")

    def test_http_status_classification_requires_an_exact_three_digit_boundary(self):
        for adjacent in (
            "HTTP 4010",
            "HTTP 4030",
            "HTTP 4040",
            "HTTP 4290",
            "HTTP 5000",
            "HTTP 5020",
            "HTTP 5030",
            "HTTP 5040",
        ):
            with self.subTest(adjacent=adjacent):
                classification = GitHubTransportSource._classify_command_failure(
                    CommandExited("/usr/bin/gh", 1, "", adjacent),
                    authenticated=False,
                )
                self.assertEqual(
                    classification,
                    (
                        "COMMAND_EXIT_TERMINAL",
                        "TRANSPORT_OBJECT_COMMAND_FAILED",
                        False,
                        False,
                    ),
                )

    def test_captured_rc10_two_timeout_failure_is_repaired_without_credential_switch(
        self,
    ):
        fixture = RC10_CAPTURED_TIMEOUT_FIXTURE
        self.assertEqual(fixture["version"], "v1.1.0-rc.10")
        self.assertEqual(fixture["repository"], "yanyuhanyue/AniMemo")
        self.assertEqual(fixture["objects"], RELEASE_BUNDLE_OBJECTS)
        self.assertEqual(fixture["installer_materials_size"], 62_484_480)
        self.assertEqual(fixture["timeout_seconds"], 60)
        self.assertEqual(fixture["attempt_modes"], ("anonymous", "authenticated"))
        self.assertEqual(
            fixture["exception_types"],
            (
                "TransportError",
                "CommandFailed",
                "TimeoutExpired",
                "CommandFailed",
                "TimeoutExpired",
            ),
        )
        self.assertEqual(fixture["legacy_final_code"], "TRANSPORT_UNAVAILABLE")
        self.assertEqual(
            fixture["exception_messages"][0], "GitHub transport is unavailable"
        )
        self.assertEqual(
            fixture["exception_messages"][1],
            "command failed: /usr/bin/gh; stdout=; stderr=",
        )
        self.assertEqual(
            fixture["exception_messages"][1], fixture["exception_messages"][3]
        )
        self.assertEqual(
            fixture["exception_messages"][2], fixture["exception_messages"][4]
        )
        self.assertIn("timed out after 60 seconds", fixture["exception_messages"][2])
        self.assertEqual(fixture["exception_messages"][2].count("'--pattern'"), 4)

        bodies = dict(self.bodies)
        bodies["installer-materials.tar"] = b"x"
        plans = tuple(
            TransportObjectPlan(
                name,
                62_484_480 if name == "installer-materials.tar" else len(bodies[name]),
            )
            for name in RELEASE_BUNDLE_OBJECTS
        )
        request = TransportRequest.release_bundle("1.1.0-rc.10", object_plans=plans)
        runner = Rc10CapturedFailureRunner(bodies)
        token_reads = []
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                credential_provider=lambda: token_reads.append(True) or "forbidden",
                sleeper=lambda _: None,
            ).acquire(request, staging)

        names = [call[0][call[0].index("--pattern") + 1] for call in runner.calls]
        installer_calls = [
            call for call in runner.calls if "installer-materials.tar" in call[0]
        ]
        self.assertEqual(names.count("checksums.txt"), 1)
        self.assertEqual(names.count("deployment-contract.json"), 1)
        self.assertEqual(names.count("installer-materials.tar"), 3)
        self.assertEqual(names.count("release-manifest.json"), 1)
        self.assertEqual(
            [call[1]["timeout"] for call in installer_calls], [507, 507, 507]
        )
        self.assertEqual(token_reads, [])
        self.assertEqual(
            acquired.diagnostics.objects[2].failure_classes,
            ("COMMAND_TIMEOUT", "COMMAND_TIMEOUT"),
        )
        self.assertEqual(acquired.diagnostics.objects[2].credential_transition_count, 0)

    def test_success_uses_four_ordered_single_pattern_commands_and_atomic_commit(self):
        runner = RecordingGitHubRunner(self.bodies)
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(runner=runner).acquire(
                self.request, staging
            )

            self.assertEqual(len(runner.calls), 4)
            self.assertEqual(
                [call[0][call[0].index("--pattern") + 1] for call in runner.calls],
                list(RELEASE_BUNDLE_OBJECTS),
            )
            self.assertTrue(
                all(call[0].count("--pattern") == 1 for call in runner.calls)
            )
            self.assertTrue(all("--clobber" not in call[0] for call in runner.calls))
            self.assertTrue(
                all("GH_TOKEN" not in call[1]["env"] for call in runner.calls)
            )
            self.assertEqual(
                [item.name for item in staging.iterdir()], [acquired.root.name]
            )
            self.assertEqual(
                [
                    acquired.material(name).read_bytes()
                    for name in RELEASE_BUNDLE_OBJECTS
                ],
                [self.bodies[name] for name in RELEASE_BUNDLE_OBJECTS],
            )

    def test_timeout_retries_same_object_without_credential_transition_or_redownload(
        self,
    ):
        timeout = CommandTimedOut("/usr/bin/gh", 60, "", "")
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"installer-materials.tar": [timeout]},
        )
        token_reads = []
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                credential_provider=lambda: (
                    token_reads.append(True) or "must-not-be-read"
                ),
                sleeper=lambda _: None,
            ).acquire(self.request, staging)

        names = [call[0][call[0].index("--pattern") + 1] for call in runner.calls]
        self.assertEqual(names.count("checksums.txt"), 1)
        self.assertEqual(names.count("deployment-contract.json"), 1)
        self.assertEqual(names.count("installer-materials.tar"), 2)
        self.assertEqual(names.count("release-manifest.json"), 1)
        self.assertEqual(token_reads, [])
        self.assertEqual(acquired.diagnostics.objects[2].credential_transition_count, 0)

    def test_anonymous_auth_boundary_allows_only_one_env_only_transition(self):
        auth = CommandExited("/usr/bin/gh", 1, "", "HTTP 401: authentication required")
        runner = RecordingGitHubRunner(self.bodies, outcomes={"checksums.txt": [auth]})
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                credential_provider=lambda: "ephemeral-read-token",
                sleeper=lambda _: None,
            ).acquire(self.request, staging)

        first, second = runner.calls[:2]
        self.assertNotIn("ephemeral-read-token", first[0])
        self.assertNotIn("GH_TOKEN", first[1]["env"])
        self.assertNotIn("ephemeral-read-token", second[0])
        self.assertEqual(second[1]["env"]["GH_TOKEN"], "ephemeral-read-token")
        self.assertNotIn("ephemeral-read-token", repr(acquired.diagnostics))
        self.assertEqual(acquired.diagnostics.objects[0].credential_transition_count, 1)

    def test_generic_429_retries_in_the_same_mode_without_reading_a_token(self):
        limited = CommandExited("/usr/bin/gh", 1, "", "HTTP 429 rate limit exceeded")
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"checksums.txt": [limited]},
        )
        delays = []
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                credential_provider=lambda: self.fail("credential must not be read"),
                sleeper=delays.append,
            ).acquire(self.request, staging)

        self.assertEqual(delays, [10])
        self.assertNotIn("GH_TOKEN", runner.calls[0][1]["env"])
        self.assertNotIn("GH_TOKEN", runner.calls[1][1]["env"])
        self.assertEqual(acquired.diagnostics.objects[0].credential_transition_count, 0)
        self.assertEqual(
            acquired.diagnostics.objects[0].failure_classes,
            ("ANONYMOUS_HTTP_429",),
        )

    def test_429_with_explicit_auth_benefit_allows_one_transition(self):
        limited = CommandExited(
            "/usr/bin/gh",
            1,
            "",
            "HTTP 429: authenticated requests get a higher rate limit",
        )
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"checksums.txt": [limited]},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                credential_provider=lambda: "ephemeral-read-token",
                sleeper=lambda _: None,
            ).acquire(self.request, staging)

        self.assertNotIn("GH_TOKEN", runner.calls[0][1]["env"])
        self.assertEqual(runner.calls[1][1]["env"]["GH_TOKEN"], "ephemeral-read-token")
        self.assertEqual(acquired.diagnostics.objects[0].credential_transition_count, 1)

    def test_authenticated_429_retries_without_returning_to_anonymous(self):
        auth_required = CommandExited(
            "/usr/bin/gh", 1, "", "HTTP 401 authentication required"
        )
        limited = CommandExited("/usr/bin/gh", 1, "", "HTTP 429 rate limit exceeded")
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"checksums.txt": [auth_required, limited]},
        )
        delays = []
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                credential_provider=lambda: "ephemeral-read-token",
                sleeper=delays.append,
            ).acquire(self.request, staging)

        self.assertEqual(delays, [30])
        self.assertNotIn("GH_TOKEN", runner.calls[0][1]["env"])
        self.assertTrue(
            all(
                call[1]["env"].get("GH_TOKEN") == "ephemeral-read-token"
                for call in runner.calls[1:3]
            )
        )
        self.assertEqual(acquired.diagnostics.objects[0].credential_transition_count, 1)

    def test_auth_boundary_on_last_attempt_fails_with_a_stable_code(self):
        outcomes = [
            CommandTimedOut("/usr/bin/gh", 60, "", ""),
            CommandTimedOut("/usr/bin/gh", 60, "", ""),
            CommandExited("/usr/bin/gh", 1, "", "HTTP 401 authentication required"),
        ]
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"checksums.txt": outcomes},
        )
        token_reads = []
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(
                    runner=runner,
                    credential_provider=lambda: token_reads.append(True) or "unused",
                    sleeper=lambda _: None,
                ).acquire(self.request, staging)

            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_OBJECT_AUTHENTICATION_REQUIRED",
            )
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(token_reads, [])
            self.assertEqual(list(staging.iterdir()), [])

    def test_authenticated_401_and_403_are_terminal_and_do_not_fall_back(self):
        for authenticated_diagnostic in (
            "HTTP 401 authentication failed",
            "HTTP 403 permission denied",
        ):
            with (
                self.subTest(diagnostic=authenticated_diagnostic),
                tempfile.TemporaryDirectory() as temporary,
            ):
                first = CommandExited(
                    "/usr/bin/gh",
                    1,
                    "",
                    "HTTP 401 authentication required",
                )
                second = CommandExited("/usr/bin/gh", 1, "", authenticated_diagnostic)
                runner = RecordingGitHubRunner(
                    self.bodies,
                    outcomes={"checksums.txt": [first, second]},
                )
                staging = Path(temporary).resolve() / "private"
                staging.mkdir(mode=0o700)
                with self.assertRaises(TransportError) as raised:
                    GitHubTransportSource(
                        runner=runner,
                        credential_provider=lambda: "ephemeral-read-token",
                        sleeper=lambda _: None,
                    ).acquire(self.request, staging)

                self.assertEqual(
                    raised.exception.code,
                    "TRANSPORT_OBJECT_AUTHENTICATION_FAILED",
                )
                self.assertEqual(len(runner.calls), 2)
                self.assertEqual(list(staging.iterdir()), [])

    def test_404_unknown_error_size_mismatch_and_unexpected_file_fail_closed(self):
        cases = (
            (
                CommandExited("/usr/bin/gh", 1, "", "HTTP 404 asset not found"),
                False,
                "TRANSPORT_OBJECT_MISSING",
            ),
            (
                CommandExited("/usr/bin/gh", 1, "", "unclassified gh failure"),
                False,
                "TRANSPORT_OBJECT_COMMAND_FAILED",
            ),
            (
                CommandExited("/usr/bin/gh", 1, "", "HTTP 400 invalid tag"),
                False,
                "TRANSPORT_OBJECT_COMMAND_FAILED",
            ),
            (
                CommandExited("/usr/bin/gh", 1, "", "invalid repository"),
                False,
                "TRANSPORT_OBJECT_COMMAND_FAILED",
            ),
            (
                CommandExited("/usr/bin/gh", 1, "", "manifest unknown"),
                False,
                "TRANSPORT_OBJECT_COMMAND_FAILED",
            ),
            (OSError("disk full"), False, "TRANSPORT_OBJECT_COMMAND_FAILED"),
            (PermissionError("denied"), False, "TRANSPORT_OBJECT_COMMAND_FAILED"),
            (None, True, "TRANSPORT_OBJECT_SET_INVALID"),
        )
        for error, extra_file, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                outcomes = {"checksums.txt": [error]} if error else None
                runner = RecordingGitHubRunner(
                    self.bodies, outcomes, extra_file=extra_file
                )
                staging = Path(temporary).resolve() / "private"
                staging.mkdir(mode=0o700)
                with self.assertRaises(TransportError) as raised:
                    GitHubTransportSource(
                        runner=runner, sleeper=lambda _: None
                    ).acquire(self.request, staging)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(list(staging.iterdir()), [])

        wrong = dict(self.bodies)
        wrong["checksums.txt"] += b"x"
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(runner=RecordingGitHubRunner(wrong)).acquire(
                    self.request, staging
                )
            self.assertEqual(raised.exception.code, "TRANSPORT_OBJECT_SIZE_MISMATCH")
            self.assertEqual(list(staging.iterdir()), [])

    def test_retryable_exit_matrix_is_bounded_in_the_same_credential_mode(self):
        cases = (
            "HTTP 500 internal server error",
            "HTTP 502 bad gateway",
            "HTTP 503 service unavailable",
            "HTTP 504 gateway timeout",
            "connection reset by peer",
            "unexpected EOF",
            "TLS handshake timeout",
            "temporary failure in name resolution",
        )
        for diagnostic in cases:
            with (
                self.subTest(diagnostic=diagnostic),
                tempfile.TemporaryDirectory() as temporary,
            ):
                runner = RecordingGitHubRunner(
                    self.bodies,
                    outcomes={
                        "checksums.txt": [
                            CommandExited("/usr/bin/gh", 1, "", diagnostic)
                        ]
                    },
                )
                delays = []
                staging = Path(temporary).resolve() / "private"
                staging.mkdir(mode=0o700)
                acquired = GitHubTransportSource(
                    runner=runner,
                    credential_provider=lambda: self.fail(
                        "credential must not be read"
                    ),
                    sleeper=delays.append,
                ).acquire(self.request, staging)

                self.assertEqual(len(runner.calls), 5)
                self.assertEqual(delays, [10])
                self.assertEqual(acquired.diagnostics.objects[0].attempt_count, 2)
                self.assertEqual(
                    acquired.diagnostics.objects[0].credential_transition_count,
                    0,
                )

    def test_three_timeouts_exhaust_without_token_or_unbounded_retry(self):
        timeouts = [CommandTimedOut("/usr/bin/gh", 60, "", "") for _ in range(3)]
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"checksums.txt": timeouts},
        )
        delays = []
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(
                    runner=runner,
                    credential_provider=lambda: self.fail(
                        "credential must not be read"
                    ),
                    sleeper=delays.append,
                ).acquire(self.request, staging)

            self.assertEqual(raised.exception.code, "TRANSPORT_OBJECT_TIMEOUT")
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(delays, [10, 30])
            self.assertEqual(list(staging.iterdir()), [])

    def test_partial_attempt_is_deleted_and_never_reused(self):
        timeout = CommandTimedOut("/usr/bin/gh", 60, "", "")
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"installer-materials.tar": [("partial", timeout)]},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                sleeper=lambda _: None,
            ).acquire(self.request, staging)

            installer_attempts = [
                call[0][call[0].index("--dir") + 1]
                for call in runner.calls
                if call[0][call[0].index("--pattern") + 1] == "installer-materials.tar"
            ]
            self.assertEqual(len(installer_attempts), 2)
            self.assertEqual(len(set(installer_attempts)), 2)
            self.assertTrue(all(not Path(path).exists() for path in installer_attempts))
            self.assertEqual(
                acquired.material("installer-materials.tar").read_bytes(),
                self.bodies["installer-materials.tar"],
            )

    def test_last_object_failure_removes_completed_pending_bundle(self):
        missing = CommandExited("/usr/bin/gh", 1, "", "HTTP 404 asset not found")
        runner = RecordingGitHubRunner(
            self.bodies,
            outcomes={"release-manifest.json": [missing]},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(runner=runner).acquire(self.request, staging)

            self.assertEqual(raised.exception.code, "TRANSPORT_OBJECT_MISSING")
            self.assertEqual(len(runner.calls), 4)
            self.assertEqual(list(staging.iterdir()), [])

    def test_missing_directory_symlink_and_hardlink_outputs_are_rejected(self):
        expected_codes = {
            "missing": "TRANSPORT_OBJECT_SET_INVALID",
            "directory": "TRANSPORT_PATH_UNSAFE",
            "symlink": "TRANSPORT_PATH_UNSAFE",
            "hardlink": "TRANSPORT_PATH_UNSAFE",
        }
        for unsafe_kind, expected_code in expected_codes.items():
            with (
                self.subTest(unsafe_kind=unsafe_kind),
                tempfile.TemporaryDirectory() as temporary,
            ):
                staging = Path(temporary).resolve() / "private"
                staging.mkdir(mode=0o700)
                with self.assertRaises(TransportError) as raised:
                    GitHubTransportSource(
                        runner=RecordingGitHubRunner(
                            self.bodies,
                            unsafe_kind=unsafe_kind,
                        )
                    ).acquire(self.request, staging)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(list(staging.iterdir()), [])

    def test_receipt_identity_is_stable_across_retry_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            direct_root = base / "direct"
            retry_root = base / "retry"
            direct_root.mkdir(mode=0o700)
            retry_root.mkdir(mode=0o700)
            direct = GitHubTransportSource(
                runner=RecordingGitHubRunner(self.bodies)
            ).acquire(self.request, direct_root)
            retry = GitHubTransportSource(
                runner=RecordingGitHubRunner(
                    self.bodies,
                    outcomes={
                        "installer-materials.tar": [
                            CommandTimedOut("/usr/bin/gh", 60, "", "")
                        ]
                    },
                ),
                sleeper=lambda _: None,
            ).acquire(self.request, retry_root)

            self.assertEqual(direct.receipt.identity, retry.receipt.identity)
            self.assertNotEqual(
                direct.diagnostics.objects[2].attempt_count,
                retry.diagnostics.objects[2].attempt_count,
            )

    def test_bundle_deadline_prevents_retry_and_cleans_pending_state(self):
        clock = FakeMonotonic()
        runner = AdvancingTimeoutRunner(self.bodies, clock, 1795)
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with self.assertRaises(TransportError) as raised:
                GitHubTransportSource(
                    runner=runner,
                    clock=clock,
                    sleeper=lambda seconds: clock.advance(seconds),
                ).acquire(self.request, staging)

            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
            )
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(list(staging.iterdir()), [])

    def test_successful_command_cannot_commit_after_postprocessing_crosses_deadline(
        self,
    ):
        clock = FakeMonotonic()
        runner = RecordingGitHubRunner(self.bodies)
        real_fsync_file = transport_module._fsync_file

        def slow_fsync(path):
            real_fsync_file(path)
            clock.advance(1801)

        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            with (
                patch("updater.transport._fsync_file", side_effect=slow_fsync),
                self.assertRaises(TransportError) as raised,
            ):
                GitHubTransportSource(
                    runner=runner,
                    clock=clock,
                    sleeper=lambda seconds: clock.advance(seconds),
                ).acquire(self.request, staging)

            self.assertEqual(
                raised.exception.code,
                "TRANSPORT_BUNDLE_DEADLINE_EXHAUSTED",
            )
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(list(staging.iterdir()), [])

    def test_post_rename_fsync_failure_removes_final_bundle_for_both_transports(self):
        cases = (
            (
                GitHubTransportSource(runner=RecordingGitHubRunner(self.bodies)),
                [None, None, None, None, None, OSError("fsync failed")],
            ),
            (
                OfficialMirrorTransportSource(opener=FakeOpener(self.bodies)),
                [None, OSError("fsync failed")],
            ),
        )
        for source, fsync_results in cases:
            with (
                self.subTest(source=source.transport_id),
                tempfile.TemporaryDirectory() as temporary,
            ):
                staging = Path(temporary).resolve() / "private"
                staging.mkdir(mode=0o700)
                with (
                    patch(
                        "updater.transport._fsync_directory",
                        side_effect=fsync_results,
                    ),
                    self.assertRaises(OSError),
                ):
                    source.acquire(self.request, staging)
                self.assertEqual(list(staging.iterdir()), [])

    def test_transport_constructors_do_not_accept_arbitrary_timeout_input(self):
        with self.assertRaises(TypeError):
            GitHubTransportSource(
                runner=RecordingGitHubRunner(self.bodies), timeout_seconds=300
            )
        with self.assertRaises(TypeError):
            OfficialMirrorTransportSource(
                opener=FakeOpener(self.bodies), timeout_seconds=300
            )

    def test_rc10_slow_transfer_passes_new_budget_without_credential_transition(self):
        rc10_bodies = dict(self.bodies)
        rc10_bodies["installer-materials.tar"] = (
            b"x"  # size is simulated, not allocated
        )
        plans = tuple(
            TransportObjectPlan(
                name,
                62_484_480
                if name == "installer-materials.tar"
                else len(rc10_bodies[name]),
            )
            for name in RELEASE_BUNDLE_OBJECTS
        )
        request = TransportRequest.release_bundle("1.1.0-rc.10", object_plans=plans)
        clock = FakeMonotonic()
        runner = SlowGitHubRunner(
            rc10_bodies,
            clock,
            {"installer-materials.tar": 477},
        )
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary).resolve() / "private"
            staging.mkdir(mode=0o700)
            acquired = GitHubTransportSource(
                runner=runner,
                clock=clock,
                sleeper=lambda seconds: clock.advance(seconds),
            ).acquire(request, staging)
            installer_call = next(
                call
                for call in runner.calls
                if call[0][call[0].index("--pattern") + 1] == "installer-materials.tar"
            )
            self.assertGreater(477, 60, "the captured legacy budget would time out")
            self.assertEqual(installer_call[1]["timeout"], 507)
            self.assertNotIn("GH_TOKEN", installer_call[1]["env"])
            self.assertEqual(
                acquired.material("installer-materials.tar").stat().st_size, 62_484_480
            )
            self.assertLessEqual(acquired.diagnostics.elapsed_milliseconds, 1_800_000)


if __name__ == "__main__":
    unittest.main()
