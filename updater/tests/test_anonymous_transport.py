"""Exact anonymous asset carrier: transport bytes remain untrusted until crypto."""

import hashlib
import io
import time
import unittest
from dataclasses import replace
from unittest import mock

from bootstrap_kit import http_supervisor as supervisor
from bootstrap_kit.http_protocol import HttpFailure
from bootstrap_kit.safe_files import (
    create_private_directory,
    directory_identity,
    remove_owned_directory,
)
from bootstrap_kit.tests.test_http_supervisor import (
    ROOT,
    loopback_program,
    test_command,
)
from installer.tests.test_anonymous_release_transport import Response, wire_server
from updater import transport


class ReapedBytes:
    """Injected already-reaped transport boundary, no verification capability."""

    def __init__(self, body):
        self.stream = io.BytesIO(body)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True
        self.stream.close()


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.results = []

    def fetch(self, selection, *, deadline):
        self.calls.append((selection, deadline))
        body = self.responses.pop(0)
        if isinstance(body, BaseException):
            raise body
        result = ReapedBytes(body)
        self.results.append(result)
        return result


def request(body=b"{}"):
    names = transport.RELEASE_BUNDLE_OBJECTS
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return transport.TransportRequest.release_bundle(
        "2.0.0-rc.3",
        object_plans=tuple(
            transport.TransportObjectPlan(name, len(body)) for name in names
        ),
        github_release_id=392113678,
        github_assets=tuple(
            transport.GitHubAssetPlan(name, 574986486 + index, digest)
            for index, name in enumerate(names)
        ),
    )


class AnonymousTransportTests(unittest.TestCase):
    def setUp(self):
        self.root = create_private_directory(ROOT, prefix="animemo-carrier-tests-")
        self.identity = directory_identity(self.root)
        self.staging = create_private_directory(self.root, prefix="staging-")

    def tearDown(self):
        remove_owned_directory(self.root, self.identity)

    def test_exact_ids_and_digests_are_required_and_bound_into_request_identity(self):
        original = request()
        for changes in (
            {"asset_id": True},
            {"asset_id": 0},
            {"sha256": "sha1:" + "a" * 40},
            {"logical_name": "../installer-materials.tar"},
        ):
            with self.assertRaises(transport.TransportError):
                replace(original.github_assets[0], **changes)
        for changes in (
            {"github_release_id": True},
            {"github_assets": ()},
            {
                "github_assets": tuple(
                    replace(item, asset_id=1) for item in original.github_assets
                )
            },
        ):
            with self.assertRaises(transport.TransportError):
                replace(original, **changes)
        altered = replace(original, github_release_id=123)
        self.assertNotEqual(original.identity, altered.identity)
        altered = replace(
            original,
            github_assets=(
                replace(original.github_assets[0], asset_id=123),
                *original.github_assets[1:],
            ),
        )
        self.assertNotEqual(original.identity, altered.identity)
        without = transport.TransportRequest.release_bundle(
            "2.0.0-rc.3", object_plans=original.object_plans
        )
        client = Client([])
        with self.assertRaises(transport.TransportError):
            transport.AnonymousGitHubTransportSource(http_client=client).acquire(
                without, self.staging
            )
        self.assertEqual(client.calls, [])

    def test_reaped_stream_is_recopied_hashed_and_all_exact_selections_preserved(self):
        original = request()
        client = Client([b"{}"] * 4)
        carrier = transport.AnonymousGitHubTransportSource(http_client=client)
        before = time.monotonic()
        result = carrier.acquire(original, self.staging)
        self.assertEqual(result.receipt.request_identity, original.identity)
        self.assertEqual(
            tuple(item.logical_name for item in result.objects),
            transport.RELEASE_BUNDLE_OBJECTS,
        )
        for index, (selection, deadline) in enumerate(client.calls):
            self.assertEqual(selection.kind, "GITHUB_ASSET")
            self.assertEqual(selection.fields["release_id"], 392113678)
            self.assertEqual(
                selection.fields["asset_id"], original.github_assets[index].asset_id
            )
            self.assertEqual(
                selection.fields["sha256"],
                original.github_assets[index].sha256.removeprefix("sha256:"),
            )
            self.assertLessEqual(deadline - before, 61)
            self.assertEqual(
                result.material(selection.fields["name"]).read_bytes(), b"{}"
            )
        self.assertTrue(all(item.closed for item in client.results))
        self.assertTrue(
            all(
                item.credential_transition_count == 0
                for item in result.diagnostics.objects
            )
        )

    def test_hash_size_and_failure_never_commit_receipt(self):
        for bad in (b"{]", b"{", b"{}x", HttpFailure("HTTP_403", http_status=403)):
            client = Client([bad])
            carrier = transport.AnonymousGitHubTransportSource(http_client=client)
            with (
                self.subTest(kind=type(bad).__name__),
                self.assertRaises(transport.TransportError),
            ):
                carrier.acquire(request(), self.staging)
            self.assertEqual(list(self.staging.iterdir()), [])
            self.assertIsNone(carrier.last_diagnostics)
            self.assertEqual(len(client.calls), 1)
            self.assertTrue(all(item.closed for item in client.results))

    def test_failed_second_attempt_does_not_leave_previous_pass_diagnostics(self):
        client = Client([b"{}"] * 4 + [HttpFailure("DEADLINE")])
        carrier = transport.AnonymousGitHubTransportSource(http_client=client)
        carrier.acquire(request(), self.staging)
        with self.assertRaises(transport.TransportError):
            carrier.acquire(request(), self.staging)
        self.assertTrue(
            carrier.last_diagnostics is None
            or carrier.last_diagnostics.result != "PASS"
        )

    def test_copy_deadline_is_checked_after_worker_result(self):
        clock = [0.0]
        client = Client([b"{}"])
        original_fetch = client.fetch

        def fetch(selection, *, deadline):
            result = original_fetch(selection, deadline=deadline)
            clock[0] = deadline + 1
            return result

        client.fetch = fetch
        with mock.patch.object(
            transport.time, "monotonic", side_effect=lambda: clock[0]
        ):
            carrier = transport.AnonymousGitHubTransportSource(http_client=client)
            with self.assertRaisesRegex(transport.TransportError, "bound"):
                carrier.acquire(request(), self.staging)
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.results[0].closed)

    def test_real_four_workers_are_reaped_before_carrier_copies_streams(self):
        workers = supervisor.SupervisedAnonymousHttp(private_root=self.root)
        actual_fetch = workers.fetch
        receipts = []

        def observed_fetch(selection, *, deadline):
            result = actual_fetch(selection, deadline=deadline)
            self.assertTrue(result.process_receipt["root_reaped"])
            self.assertTrue(result.process_receipt["tree_empty"])
            receipts.append(result.process_receipt)
            return result

        workers.fetch = observed_fetch
        responses = [
            Response(
                body=b"{}",
                headers=[
                    ("Content-Type", "application/octet-stream"),
                    ("Content-Length", "2"),
                ],
            )
            for _ in range(4)
        ]
        with (
            wire_server(responses) as (port, calls),
            mock.patch.object(
                supervisor, "_command", test_command(loopback_program(port))
            ),
        ):
            result = transport.AnonymousGitHubTransportSource(
                http_client=workers
            ).acquire(request(), self.staging)
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(receipts), 4)
            for name in transport.RELEASE_BUNDLE_OBJECTS:
                self.assertEqual(result.material(name).read_bytes(), b"{}")
