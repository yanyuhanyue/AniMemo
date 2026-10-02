"""Release-domain tests at the supervised client seam, plus real worker wire cases."""

from __future__ import annotations

import copy
import http.server
import io
import json
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from bootstrap_kit import http_supervisor as supervisor
from bootstrap_kit.http_protocol import HttpFailure, HttpSelection
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
from installer import anonymous_release_transport as transport

VERSION = "v2.0.0-rc.3"
TAG = "a" * 40
COMMIT = "b" * 40
CANARY = "NONSECRET_CREDENTIAL_CANARY_MUST_NOT_BE_READ_OR_SENT"


def raw(value):
    return json.dumps(value, separators=(",", ":")).encode()


class Response:
    def __init__(self, value=None, *, body=None, status=200, headers=None):
        self.body = raw(value) if body is None else body
        self.status = status
        self.headers = (
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(self.body))),
            ]
            if headers is None
            else headers
        )


class Result:
    """Plain test bytes at an already-reaped HTTP-object boundary, never crypto."""

    def __init__(self, response):
        self.status = response.status
        self.headers = {k.lower(): v for k, v in response.headers}
        self.body = response.body
        self.stream = io.BytesIO(self.body)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True
        self.stream.close()

    def read_bytes(self, maximum):
        if len(self.body) > maximum:
            raise HttpFailure("TOO_LARGE")
        return self.body


class Network:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.connections = []

    def fetch(self, selection, *, deadline):
        assert type(selection) is HttpSelection and selection.kind == "GITHUB_JSON"
        assert deadline > time.monotonic()
        self.requests.append(selection)
        if not self.responses:
            raise AssertionError("Unexpected request or fallback")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        result = Result(response)
        self.connections.append(result)
        return result


@contextmanager
def wire_server(responses):
    """Loopback real HTTP responses, passed through an actual owned worker."""
    pending = list(responses)
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            calls.append((self.path, list(self.headers.items())))
            response = pending.pop(0)
            self.send_response(response.status)
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(response.body)
            except OSError:
                pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="animemo-domain-http")
    thread.start()
    try:
        yield server.server_port, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError("Owned loopback server did not stop")


def release():
    names = [
        "checksums.txt",
        "deployment-contract.json",
        "installer-materials.tar",
        "release-manifest.json",
        f"animemo-{VERSION}-portable.tar",
    ]
    return {
        "id": 392113678,
        "tag_name": VERSION,
        "draft": False,
        "prerelease": True,
        "immutable": True,
        "published_at": "2026-09-19T15:21:17Z",
        "assets": [
            {
                "id": i + 1,
                "name": name,
                "state": "uploaded",
                "size": i + 1,
                "digest": "sha256:" + str(i + 1) * 64,
            }
            for i, name in enumerate(names)
        ],
    }


def reference():
    return {
        "ref": "refs/tags/" + VERSION,
        "object": {
            "type": "tag",
            "sha": TAG,
            "url": f"https://{transport.HOST}{transport.BASE}/git/tags/{TAG}",
        },
    }


def annotated():
    return {
        "sha": TAG,
        "tag": VERSION,
        "object": {
            "type": "commit",
            "sha": COMMIT,
            "url": f"https://{transport.HOST}{transport.BASE}/git/commits/{COMMIT}",
        },
    }


def proof():
    return {
        "attestations": [
            {
                "repository_id": transport.REPOSITORY_ID,
                "initiator": "github",
                "bundle_url": "https://not-fetched.invalid/private?" + CANARY,
                "bundle": {
                    "mediaType": "UNTRUSTED_SYNTHETIC",
                    "dsseEnvelope": {"payload": "unverified"},
                },
            }
        ]
    }


def prefix():
    return [Response(release()), Response(reference()), Response(annotated())]


class AnonymousReleaseTransportTests(unittest.TestCase):
    def fetch(self, responses):
        network = Network(responses)
        self.addCleanup(
            lambda: self.assertTrue(all(c.closed for c in network.connections))
        )
        return transport.AnonymousReleaseReader(http_client=network).fetch(
            VERSION
        ), network

    def failure(self, responses, suffix, *, request_count=None):
        network = Network(responses)
        with self.assertRaises(transport.AnonymousReleaseError) as caught:
            transport.AnonymousReleaseReader(http_client=network).fetch(VERSION)
        self.assertEqual(caught.exception.code, "BOOTSTRAP_ANONYMOUS_" + suffix)
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertTrue(all(c.closed for c in network.connections))
        if request_count is not None:
            self.assertEqual(len(network.requests), request_count)
        return caught.exception

    def test_untrusted_complete_fetch_does_not_return_or_request_signed_url(self):
        dto, network = self.fetch(prefix() + [Response(proof())])
        self.assertEqual((dto.tag_object, dto.tag_commit), (TAG, COMMIT))
        self.assertEqual(len(network.requests), 4)
        self.assertNotIn(CANARY, repr(dto))
        self.assertNotIn("bundle_url", dto.release_bundle.decode())
        self.assertNotIn("verified", vars(dto))
        self.assertTrue(
            all(s.target()[0] == "api.github.com" for s in network.requests)
        )

    def test_supervisor_failure_and_cleanup_codes_preserve_http_identity(self):
        for status in (401, 403, 404, 429, 500, 503, 302):
            error = HttpFailure(
                "HTTP_" + str(status),
                http_status=status,
                cleanup=("BOOTSTRAP_HTTP_JOB_CLOSE_FAILED",),
            )
            caught = self.failure([error], "HTTP_" + str(status), request_count=1)
            self.assertEqual(caught.http_status, status)
            self.assertTrue(caught.cleanup_failed)
            self.assertEqual(caught.secondary_errors, error.secondary_errors)
        self.failure([HttpFailure("DEADLINE")], "DEADLINE", request_count=1)
        self.failure([HttpFailure("NETWORK_FAILED")], "NETWORK_FAILED", request_count=1)

    def test_json_duplicate_nonfinite_malformed_and_nonobject_reject(self):
        for body in (b'{"id":1,"id":2}', b"{", b"\xff", b'{"x":NaN}', b"[]"):
            self.failure(
                [Response(body=body)],
                "RELEASE_IDENTITY_INVALID" if body == b"[]" else "JSON_INVALID",
            )

    def test_invalid_version_total_bytes_and_remaining_deadline_stop_client(self):
        for version in (
            None,
            "../user",
            "v2.0.0\r\nInjected: yes",
            "v" + "1" * 128 + ".0.0",
        ):
            client = Network([])
            with self.assertRaises(transport.AnonymousReleaseError):
                transport.AnonymousReleaseReader(http_client=client).fetch(version)
            self.assertEqual(client.requests, [])
        with mock.patch.object(transport, "MAX_TOTAL_BYTES", 8):
            self.failure([Response(release())], "RESPONSE_TOO_LARGE", request_count=1)
        with mock.patch.object(transport, "MAX_JSON_BYTES", 8):
            self.failure(
                [
                    Response(
                        body=b"x" * 9, headers=[("Content-Type", "application/json")]
                    )
                ],
                "TOO_LARGE",
            )
        reader = transport.AnonymousReleaseReader(http_client=Network([]))
        reader._deadline = time.monotonic() - 1
        with self.assertRaisesRegex(transport.AnonymousReleaseError, "TIMEOUT"):
            reader._get(transport.BASE + "/releases", "RELEASE_METADATA")

    def test_no_account_configuration_or_credential_helper_at_domain_seam(self):
        client = Network(prefix() + [Response(proof())])
        with (
            mock.patch(
                "os.environ",
                {
                    "GH_TOKEN": CANARY,
                    "GITHUB_TOKEN": CANARY,
                    "HTTPS_PROXY": CANARY,
                    "HOME": CANARY,
                },
            ),
            mock.patch(
                "builtins.open", side_effect=AssertionError("configuration read")
            ),
            mock.patch(
                "subprocess.Popen", side_effect=AssertionError("credential helper")
            ),
        ):
            dto = transport.AnonymousReleaseReader(
                http_client=client, private_root=ROOT
            ).fetch(VERSION)
        self.assertNotIn(CANARY, repr(dto))

    def test_real_supervised_four_response_release_fetch(self):
        private = create_private_directory(ROOT, prefix="animemo-reader-wire-")
        identity = directory_identity(private)
        try:
            with (
                wire_server(prefix() + [Response(proof())]) as (port, calls),
                mock.patch.object(
                    supervisor, "_command", test_command(loopback_program(port))
                ),
            ):
                dto = transport.AnonymousReleaseReader(private_root=private).fetch(
                    VERSION
                )
                self.assertEqual(dto.tag_object, TAG)
                self.assertEqual(len(calls), 4)
                for path, headers in calls:
                    values = {k.lower(): v for k, v in headers}
                    self.assertEqual(values["x-github-api-version"], "2022-11-28")
                    self.assertFalse({"authorization", "cookie"} & set(values))
                    self.assertNotIn(CANARY, path)
        finally:
            remove_owned_directory(private, identity)

    def test_wire_duplicate_headers_and_truncation_rejected_by_supervisor(self):
        for headers, expected in [
            (
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "2"),
                    ("content-length", "2"),
                ],
                "HEADERS_INVALID",
            ),
            (
                [("Content-Type", "application/json"), ("Content-Length", "3")],
                "TRUNCATED",
            ),
            (
                [("Content-Type", "application/json"), ("Content-Encoding", "gzip")],
                "ENCODING_INVALID",
            ),
        ]:
            private = create_private_directory(ROOT, prefix="animemo-reader-wire-")
            identity = directory_identity(private)
            try:
                with (
                    wire_server([Response(body=b"{}", headers=headers)]) as (
                        port,
                        calls,
                    ),
                    mock.patch.object(
                        supervisor, "_command", test_command(loopback_program(port))
                    ),
                    self.assertRaisesRegex(transport.AnonymousReleaseError, expected),
                ):
                    transport.AnonymousReleaseReader(private_root=private).fetch(
                        VERSION
                    )
                self.assertEqual(len(calls), 1)
            finally:
                remove_owned_directory(private, identity)

    def test_metadata_and_exact_asset_roles_are_required(self):
        for key, value in (
            ("immutable", False),
            ("draft", True),
            ("prerelease", False),
            ("tag_name", "v2.0.0-rc.2"),
            ("id", True),
        ):
            metadata = release()
            metadata[key] = value
            with self.subTest(key=key):
                self.failure([Response(metadata)], "RELEASE_IDENTITY_INVALID")
        for mutation in (
            "duplicate-name",
            "duplicate-id",
            "missing",
            "extra",
            "bad-size",
            "bad-digest",
        ):
            metadata = release()
            if mutation == "duplicate-name":
                metadata["assets"][1]["name"] = metadata["assets"][0]["name"]
            elif mutation == "duplicate-id":
                metadata["assets"][1]["id"] = metadata["assets"][0]["id"]
            elif mutation == "missing":
                metadata["assets"].pop()
            elif mutation == "extra":
                metadata["assets"].append(copy.deepcopy(metadata["assets"][0]))
            elif mutation == "bad-size":
                metadata["assets"][0]["size"] = True
            else:
                metadata["assets"][0]["digest"] = "sha1:" + TAG
            with self.subTest(mutation=mutation):
                self.failure([Response(metadata)], "ASSET_INVENTORY_INVALID")

    def test_annotated_reference_chain_cannot_downgrade_escape_or_cycle(self):
        for mutation in ("lightweight", "foreign-url", "wrong-tag", "cycle"):
            ref, tag = reference(), annotated()
            if mutation == "lightweight":
                ref["object"]["type"] = "commit"
            elif mutation == "foreign-url":
                ref["object"]["url"] = "https://attacker.invalid/" + CANARY
            elif mutation == "wrong-tag":
                tag["tag"] = "v2.0.0-rc.2"
            else:
                tag["object"] = ref["object"]
            with self.subTest(mutation=mutation):
                self.failure(
                    [Response(release()), Response(ref), Response(tag)], "TAG_INVALID"
                )

    def test_proof_is_unique_inline_and_platform_owned(self):
        for mutation in ("missing", "duplicate", "initiator", "repository", "url-only"):
            value = proof()
            if mutation == "missing":
                value["attestations"] = []
            elif mutation == "duplicate":
                value["attestations"] *= 2
            elif mutation == "initiator":
                value["attestations"][0]["initiator"] = "actions"
            elif mutation == "repository":
                value["attestations"][0]["repository_id"] += 1
            else:
                del value["attestations"][0]["bundle"]
            code = {
                "missing": "PROOF_MISSING",
                "duplicate": "PROOF_DUPLICATE",
                "url-only": "PROOF_INLINE_UNAVAILABLE",
            }.get(mutation, "PROOF_INVALID")
            with self.subTest(mutation=mutation):
                self.failure(prefix() + [Response(value)], code)

    def link(self, page, relation="next", *, host=transport.HOST, path=None):
        endpoint = path or transport.BASE + "/attestations/sha1:" + TAG
        return f'<https://{host}{endpoint}?per_page=100&predicate_type={transport.PREDICATE_FILTER}&page={page}>; rel="{relation}"'

    def test_complete_pagination_reads_all_pages_before_accepting_single_proof(self):
        first = Response({"attestations": []})
        first.headers.append(("Link", self.link(2)))
        dto, network = self.fetch(prefix() + [first, Response(proof())])
        self.assertEqual(len(network.requests), 5)
        self.assertTrue(network.requests[-1].fields["path"].endswith("&page=2"))
        self.assertEqual(len(dto.observations), 5)
        first = Response(proof())
        first.headers.append(("Link", self.link(2)))
        self.failure(prefix() + [first, Response(proof())], "PROOF_DUPLICATE")

    def test_pagination_rejects_foreign_endpoint_regression_missing_page_and_duplicates(
        self,
    ):
        links = [
            self.link(2, host="attacker.invalid"),
            self.link(1),
            self.link(3),
            self.link(2, path=transport.BASE + "/issues"),
            self.link(2) + "," + self.link(2),
            self.link(transport.MAX_PAGES + 1),
            self.link(2).replace("per_page=100", "per_page=99"),
            self.link(2).replace("&page=2", "&page=2&page=2"),
        ]
        for link in links:
            first = Response(proof())
            first.headers.append(("Link", link))
            with self.subTest(link_shape=link.split(">;")[1]):
                self.failure(prefix() + [first], "PAGINATION_INVALID")
        first = Response(proof())
        first.headers.append(("Link", self.link(3, relation="last")))
        self.failure(prefix() + [first], "PAGINATION_INCOMPLETE")

    def test_observed_deprecation_link_is_not_a_pagination_target(self):
        lifecycle = '<https://docs.github.com/en/rest/about-the-rest-api/api-versions>; rel="deprecation"; type="text/html"'
        only = Response(proof())
        only.headers.append(("Link", lifecycle))
        _dto, network = self.fetch(prefix() + [only])
        self.assertEqual(len(network.requests), 4)
        first = Response({"attestations": []})
        first.headers.append(("Link", lifecycle + ", " + self.link(2)))
        _dto, network = self.fetch(prefix() + [first, Response(proof())])
        self.assertEqual(len(network.requests), 5)
        for invalid in (
            lifecycle.replace("docs.github.com", "attacker.invalid"),
            lifecycle.replace("api-versions>", "api-versions?private>"),
            lifecycle + ", " + lifecycle,
        ):
            response = Response(proof())
            response.headers.append(("Link", invalid))
            self.failure(prefix() + [response], "PAGINATION_INVALID")
