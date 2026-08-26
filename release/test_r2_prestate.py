from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import tempfile
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from release import cli
from release.candidate import canonical_json_bytes, sha256_bytes
from release.r2_prestate import (
    ACCESS_KEY_ENV,
    ACCOUNT_ID_ENV,
    JURISDICTION_ENV,
    R2_AUTH_METHOD,
    R2_AUTH_METHOD_ARGUMENT,
    R2_BUCKET,
    R2_RC14_EXPECTED_KEYS,
    R2_RC14_PREFIX,
    R2_RECEIPT_SCHEMA,
    SECRET_KEY_ENV,
    SESSION_TOKEN_ENV,
    Boto3R2ReadonlyClient,
    R2S3Credentials,
    R2S3PrecheckError,
    build_r2_s3_endpoint,
    r2_origin_receipt_digest,
    sanitize_r2_diagnostic,
    validate_r2_origin_receipt,
    verify_r2_origin_empty,
    verify_rc14_r2_origin_from_environment,
    write_r2_origin_receipt,
)

ACCOUNT_ID = "a" * 32
SHA = "b" * 40
TREE = "c" * 40
ACCESS = "FAKE_ACCESS_KEY_SENTINEL"
SECRET = "FAKE_SECRET_KEY_SENTINEL"
SESSION = "FAKE_SESSION_TOKEN_SENTINEL"
SIGNATURE = "FAKE_SIGNATURE_SENTINEL"


class FakeClientError(Exception):
    def __init__(self, code: str, status: int, detail: str = "") -> None:
        self.response = {
            "Error": {"Code": code, "Message": detail},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
        super().__init__(detail or code)


class RecordingS3Client:
    def __init__(self, pages=None, heads=None, list_error=None) -> None:
        self.pages = list(pages or [{"Contents": [], "IsTruncated": False}])
        self.heads = dict(heads or {})
        self.list_error = list_error
        self.operations: list[tuple[str, dict[str, object]]] = []

    def list_objects_v2(self, *, continuation_token=None):
        self.operations.append(
            ("ListObjectsV2", {"continuation_token": continuation_token})
        )
        if self.list_error is not None:
            raise self.list_error
        if not self.pages:
            raise AssertionError("unexpected ListObjectsV2 page")
        return self.pages.pop(0)

    def head_object(self, *, key):
        self.operations.append(("HeadObject", {"key": key}))
        if key not in self.heads:
            raise FakeClientError("NoSuchKey", 404)
        value = self.heads[key]
        if isinstance(value, BaseException):
            raise value
        return value

    def get_object(self, *, key):
        self.operations.append(("GetObject", {"key": key}))
        return {"Body": io.BytesIO(b"test-only")}


def environment(**overrides: str) -> dict[str, str]:
    values = {
        ACCESS_KEY_ENV: ACCESS,
        SECRET_KEY_ENV: SECRET,
        SESSION_TOKEN_ENV: SESSION,
        ACCOUNT_ID_ENV: ACCOUNT_ID,
        JURISDICTION_ENV: "default",
    }
    values.update(overrides)
    return values


def fixed_clock():
    values = iter(
        (
            datetime(2026, 8, 26, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2026, 8, 26, 1, 2, 4, tzinfo=timezone.utc),
        )
    )
    return lambda: next(values)


class R2S3PrestateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = mock.patch(
            "release.r2_prestate.R2_ACCOUNT_ID_SHA256",
            sha256_bytes(ACCOUNT_ID.encode("ascii")),
        )
        self.identity.start()

    def tearDown(self) -> None:
        self.identity.stop()

    def verify(self, client=None, **overrides):
        return verify_r2_origin_empty(
            source_sha=overrides.pop("source_sha", SHA),
            source_tree=overrides.pop("source_tree", TREE),
            account_id=overrides.pop("account_id", ACCOUNT_ID),
            jurisdiction=overrides.pop("jurisdiction", "default"),
            credentials=overrides.pop(
                "credentials", R2S3Credentials(ACCESS, SECRET, SESSION)
            ),
            client=client or RecordingS3Client(),
            clock=overrides.pop("clock", fixed_clock()),
            **overrides,
        )

    def test_empty_prefix_passes_with_exact_list_and_head_requests(self):
        client = RecordingS3Client()
        receipt = self.verify(client)
        self.assertEqual(receipt["schema"], R2_RECEIPT_SCHEMA)
        self.assertEqual(receipt["auth_method"], R2_AUTH_METHOD)
        self.assertEqual(receipt["result"], "PROVEN_EMPTY")
        self.assertEqual(receipt["write_request_count"], 0)
        self.assertEqual(receipt["list_objects_v2_request_count"], 1)
        self.assertEqual(receipt["head_object_request_count"], 6)
        self.assertEqual(
            [name for name, _ in client.operations],
            ["ListObjectsV2"] + ["HeadObject"] * 6,
        )
        self.assertIsNone(client.operations[0][1]["continuation_token"])
        self.assertTrue(
            all(
                request["key"]
                == R2_RC14_PREFIX + R2_RC14_EXPECTED_KEYS[index]
                for index, (name, request) in enumerate(client.operations[1:])
                if name == "HeadObject"
            )
        )
        self.assertTrue(
            all(
                request["key"].startswith(R2_RC14_PREFIX)
                for name, request in client.operations
                if name == "HeadObject"
            )
        )

    def test_pagination_uses_continuation_token_and_stops_on_first_object(self):
        unicode_key = R2_RC14_PREFIX + "大小写/Anime.JSON"
        client = RecordingS3Client(
            pages=[
                {
                    "Contents": [],
                    "IsTruncated": True,
                    "NextContinuationToken": "page-2",
                },
                {
                    "Contents": [
                        {
                            "Key": unicode_key,
                            "Size": 3,
                            "ETag": '"etag"',
                            "StorageClass": "STANDARD",
                        }
                    ],
                    "IsTruncated": False,
                },
            ]
        )
        with self.assertRaises(R2S3PrecheckError) as raised:
            self.verify(client)
        self.assertEqual(raised.exception.code, "R2_S3_PREFIX_NON_EMPTY")
        self.assertEqual(
            raised.exception.safe_diagnostic["object_inventory"][0]["key"],
            unicode_key,
        )
        self.assertEqual(client.operations[1][1]["continuation_token"], "page-2")
        self.assertNotEqual(unicode_key, unicode_key.lower())
        self.assertEqual(len(client.operations), 2)

    def test_single_page_common_prefix_is_non_empty(self):
        client = RecordingS3Client(
            pages=[
                {
                    "Contents": [],
                    "CommonPrefixes": [{"Prefix": R2_RC14_PREFIX + "临时/"}],
                    "IsTruncated": False,
                }
            ]
        )
        with self.assertRaisesRegex(R2S3PrecheckError, "R2_S3_PREFIX_NON_EMPTY"):
            self.verify(client)

    def test_duplicate_or_missing_continuation_token_fails_closed(self):
        for token in (None, "same"):
            pages = [
                {
                    "Contents": [],
                    "IsTruncated": True,
                    "NextContinuationToken": token,
                }
            ]
            if token == "same":
                pages.append(
                    {
                        "Contents": [],
                        "IsTruncated": True,
                        "NextContinuationToken": "same",
                    }
                )
            with self.subTest(token=token), self.assertRaisesRegex(
                R2S3PrecheckError, "R2_S3_RESPONSE_INVALID"
            ):
                self.verify(RecordingS3Client(pages=pages))

    def test_head_object_presence_is_non_empty(self):
        key = R2_RC14_PREFIX + R2_RC14_EXPECTED_KEYS[0]
        client = RecordingS3Client(
            heads={
                key: {
                    "ContentLength": 7,
                    "ETag": '"etag"',
                    "ContentType": "application/json",
                }
            }
        )
        with self.assertRaises(R2S3PrecheckError) as raised:
            self.verify(client)
        self.assertEqual(raised.exception.code, "R2_S3_PREFIX_NON_EMPTY")
        self.assertEqual(
            raised.exception.safe_diagnostic["object_inventory"][0]["key"], key
        )

    def test_default_and_supported_jurisdiction_endpoints_are_fixed(self):
        self.assertEqual(
            build_r2_s3_endpoint(ACCOUNT_ID, "default"),
            (
                f"{ACCOUNT_ID}.r2.cloudflarestorage.com",
                f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
            ),
        )
        self.assertEqual(
            build_r2_s3_endpoint(ACCOUNT_ID, "eu")[1],
            f"https://{ACCOUNT_ID}.eu.r2.cloudflarestorage.com",
        )
        for jurisdiction in ("", "local", "https://localhost", "EU"):
            with self.subTest(jurisdiction=jurisdiction), self.assertRaisesRegex(
                R2S3PrecheckError, "R2_S3_ENDPOINT_INVALID"
            ):
                build_r2_s3_endpoint(ACCOUNT_ID, jurisdiction)

    def test_wrong_account_and_receipt_identity_substitutions_fail(self):
        with self.assertRaisesRegex(R2S3PrecheckError, "R2_S3_ACCOUNT_MISMATCH"):
            build_r2_s3_endpoint("d" * 32, "default")
        receipt = self.verify()
        for field, value in (
            ("bucket", "other"),
            ("prefix", R2_RC14_PREFIX + "other/"),
            ("source_sha", "d" * 40),
            ("source_tree", "e" * 40),
            ("auth_method", "CLOUDFLARE_REST_BEARER"),
            ("write_request_count", 1),
            ("object_count", 1),
        ):
            tampered = dict(receipt)
            tampered[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                R2S3PrecheckError, "R2_S3_RECEIPT_INVALID"
            ):
                validate_r2_origin_receipt(
                    tampered,
                    expected_source_sha=SHA,
                    expected_source_tree=TREE,
                )

    def test_dedicated_credentials_are_required_and_auth_mode_is_explicit(self):
        for missing in (ACCESS_KEY_ENV, SECRET_KEY_ENV):
            values = environment()
            values.pop(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(
                R2S3PrecheckError, "R2_S3_CREDENTIAL_MISSING"
            ):
                verify_rc14_r2_origin_from_environment(
                    source_sha=SHA,
                    source_tree=TREE,
                    auth_method=R2_AUTH_METHOD_ARGUMENT,
                    environment=values,
                    client=RecordingS3Client(),
                )
        with self.assertRaisesRegex(R2S3PrecheckError, "R2_S3_AUTH_METHOD_INVALID"):
            verify_rc14_r2_origin_from_environment(
                source_sha=SHA,
                source_tree=TREE,
                auth_method="auto",
                environment=environment(),
                client=RecordingS3Client(),
            )

    def test_optional_session_token_and_generic_aws_or_rest_env_are_not_used(self):
        values = environment()
        values.pop(SESSION_TOKEN_ENV)
        values.update(
            {
                "AWS_ACCESS_KEY_ID": "generic-access",
                "AWS_SECRET_ACCESS_KEY": "generic-secret",
                "AWS_PROFILE": "ambient-profile",
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/ambient",
                "AWS_EC2_METADATA_DISABLED": "false",
                "ANIMEMO_R2_READONLY_API_TOKEN": "rest-bearer",
            }
        )
        receipt = verify_rc14_r2_origin_from_environment(
            source_sha=SHA,
            source_tree=TREE,
            auth_method=R2_AUTH_METHOD_ARGUMENT,
            environment=values,
            client=RecordingS3Client(),
            clock=fixed_clock(),
        )
        self.assertEqual(receipt["result"], "PROVEN_EMPTY")
        missing = dict(values)
        missing.pop(ACCESS_KEY_ENV)
        with self.assertRaisesRegex(R2S3PrecheckError, "R2_S3_CREDENTIAL_MISSING"):
            verify_rc14_r2_origin_from_environment(
                source_sha=SHA,
                source_tree=TREE,
                auth_method=R2_AUTH_METHOD_ARGUMENT,
                environment=missing,
                client=RecordingS3Client(),
            )

    def test_sdk_error_classification_is_stable_and_drops_service_detail(self):
        cases = (
            ("InvalidAccessKeyId", 403, "R2_S3_AUTHENTICATION_FAILED"),
            ("SignatureDoesNotMatch", 403, "R2_S3_AUTHENTICATION_FAILED"),
            ("AccessDenied", 403, "R2_S3_PERMISSION_DENIED"),
            ("NoSuchBucket", 404, "R2_S3_BUCKET_MISMATCH"),
            ("PermanentRedirect", 301, "R2_S3_ENDPOINT_INVALID"),
            ("RequestTimeTooSkewed", 403, "R2_S3_CLOCK_SKEW"),
        )
        for code, status, expected in cases:
            detail = f"server detail {ACCESS} {SECRET} {SIGNATURE}"
            with self.subTest(code=code), self.assertRaises(R2S3PrecheckError) as raised:
                self.verify(
                    RecordingS3Client(
                        list_error=FakeClientError(code, status, detail)
                    )
                )
            self.assertEqual(raised.exception.code, expected)
            self.assertEqual(str(raised.exception), expected)
            self.assertEqual(str(raised.exception).count(detail), 0)
            self.assertIsNone(raised.exception.__context__)
            formatted = "".join(traceback.format_exception(raised.exception))
            for sentinel in (ACCESS, SECRET, SESSION, SIGNATURE):
                self.assertEqual(formatted.count(sentinel), 0)

    def test_boto_client_is_explicit_tls_s3v4_without_proxy_or_write_surface(self):
        sdk = mock.Mock()
        sdk.list_objects_v2.return_value = {"Contents": [], "IsTruncated": False}
        sdk.head_object.side_effect = FakeClientError("NoSuchKey", 404)
        sdk.get_object.return_value = {"Body": io.BytesIO(b"test")}
        boto3_session = mock.Mock()
        boto3_session.client.return_value = sdk
        key = R2_RC14_PREFIX + R2_RC14_EXPECTED_KEYS[0]
        with mock.patch("boto3.Session", return_value=boto3_session) as create:
            client = Boto3R2ReadonlyClient(
                account_id=ACCOUNT_ID,
                jurisdiction="us",
                credentials=R2S3Credentials(ACCESS, SECRET, SESSION),
            )
            client.list_objects_v2()
            with self.assertRaises(Exception) as missing:
                client.head_object(key=key)
            self.assertEqual(type(missing.exception).__name__, "_R2ObjectNotFound")
            self.assertIsNone(missing.exception.__context__)
            client.get_object(key=key)
            with self.assertRaisesRegex(R2S3PrecheckError, "R2_S3_RESPONSE_INVALID"):
                client.get_object(key=R2_RC14_PREFIX + "outside-contract")
            sdk.list_objects_v2.side_effect = FakeClientError(
                "SignatureDoesNotMatch",
                403,
                f"{ACCESS} {SECRET} {SESSION} {SIGNATURE}",
            )
            with self.assertRaises(R2S3PrecheckError) as sdk_failure:
                client.list_objects_v2()
            self.assertIsNone(sdk_failure.exception.__context__)
            formatted = "".join(traceback.format_exception(sdk_failure.exception))
            for sentinel in (ACCESS, SECRET, SESSION, SIGNATURE):
                self.assertEqual(formatted.count(sentinel), 0)
        session_kwargs = create.call_args.kwargs
        self.assertEqual(session_kwargs["aws_access_key_id"], ACCESS)
        self.assertEqual(session_kwargs["aws_secret_access_key"], SECRET)
        self.assertEqual(session_kwargs["aws_session_token"], SESSION)
        self.assertEqual(session_kwargs["region_name"], "auto")
        closed_session = session_kwargs["botocore_session"]
        self.assertIsNone(closed_session.get_config_variable("profile"))
        self.assertEqual(closed_session.get_config_variable("config_file"), os.devnull)
        self.assertEqual(
            closed_session.get_config_variable("credentials_file"), os.devnull
        )
        kwargs = boto3_session.client.call_args.kwargs
        self.assertEqual(boto3_session.client.call_args.args, ("s3",))
        self.assertEqual(kwargs["region_name"], "auto")
        self.assertEqual(
            kwargs["endpoint_url"],
            f"https://{ACCOUNT_ID}.us.r2.cloudflarestorage.com",
        )
        self.assertTrue(kwargs["use_ssl"])
        self.assertTrue(kwargs["verify"])
        self.assertEqual(kwargs["config"].proxies, {})
        for forbidden in (
            "put_object",
            "delete_object",
            "delete_objects",
            "copy_object",
            "create_multipart_upload",
            "upload_part",
            "complete_multipart_upload",
            "abort_multipart_upload",
        ):
            self.assertFalse(hasattr(Boto3R2ReadonlyClient, forbidden))

    def test_production_session_ignores_profile_files_metadata_and_generic_credentials(self):
        ambient = {
            "AWS_PROFILE": "definitely-does-not-exist",
            "AWS_DEFAULT_PROFILE": "also-does-not-exist",
            "AWS_CONFIG_FILE": str(Path("Z:/missing-aws-config")),
            "AWS_SHARED_CREDENTIALS_FILE": str(Path("Z:/missing-aws-credentials")),
            "AWS_ACCESS_KEY_ID": "generic-access",
            "AWS_SECRET_ACCESS_KEY": "generic-secret",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/ambient-ecs",
            "AWS_EC2_METADATA_DISABLED": "false",
        }
        with mock.patch.dict(os.environ, ambient, clear=False):
            client = Boto3R2ReadonlyClient(
                account_id=ACCOUNT_ID,
                jurisdiction="default",
                credentials=R2S3Credentials(ACCESS, SECRET, SESSION),
            )
        self.assertIsInstance(client, Boto3R2ReadonlyClient)

    def test_real_sdk_debug_logging_is_suppressed_before_signed_request_output(self):
        from botocore.awsrequest import AWSResponse

        class RawResponse:
            def stream(self, _amount=None, decode_content=False):
                del decode_content
                yield (
                    b'<?xml version="1.0" encoding="UTF-8"?>'
                    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    b'<Name>animemo-release-mirror</Name><Prefix></Prefix>'
                    b'<KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys>'
                    b'<IsTruncated>false</IsTruncated></ListBucketResult>'
                )

        def fake_send(_session, request):
            return AWSResponse(
                request.url,
                200,
                {"content-type": "application/xml"},
                RawResponse(),
            )

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous_level = root.level
        previous_disable = logging.root.manager.disable
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            with mock.patch(
                "botocore.httpsession.URLLib3Session.send", autospec=True
            ) as send:
                send.side_effect = fake_send
                client = Boto3R2ReadonlyClient(
                    account_id=ACCOUNT_ID,
                    jurisdiction="default",
                    credentials=R2S3Credentials(ACCESS, SECRET, SESSION),
                )
                response = client.list_objects_v2()
                self.assertEqual(response.get("Contents", []), [])
                self.assertEqual(send.call_count, 1)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
        self.assertEqual(logging.root.manager.disable, previous_disable)
        output = stream.getvalue()
        for sentinel in (ACCESS, SECRET, SESSION, SIGNATURE):
            self.assertEqual(output.count(sentinel), 0)
        self.assertEqual(output.count("CanonicalRequest"), 0)
        self.assertEqual(output.count("Signature:"), 0)

    def test_receipt_is_deterministic_stable_closed_and_immutable(self):
        first = self.verify()
        second = self.verify()
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(r2_origin_receipt_digest(first), r2_origin_receipt_digest(second))
        receipt_text = canonical_json_bytes(first).decode("utf-8")
        self.assertEqual(receipt_text.count(ACCESS), 0)
        self.assertEqual(receipt_text.count(SECRET), 0)
        self.assertEqual(receipt_text.count(SESSION), 0)
        secret_field = dict(first)
        secret_field["access_key_id"] = ACCESS
        with self.assertRaisesRegex(R2S3PrecheckError, "R2_S3_RECEIPT_INVALID"):
            validate_r2_origin_receipt(secret_field)
        rest_receipt = dict(first)
        rest_receipt["schema"] = "animemo.r2-rest-prestate-receipt/v1"
        rest_receipt["auth_method"] = "CLOUDFLARE_REST_BEARER"
        with self.assertRaisesRegex(R2S3PrecheckError, "R2_S3_RECEIPT_INVALID"):
            validate_r2_origin_receipt(rest_receipt)
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "receipt.json"
            first_digest = write_r2_origin_receipt(target, first)
            self.assertEqual(first_digest, sha256_bytes(target.read_bytes()))
            self.assertEqual(write_r2_origin_receipt(target, first), first_digest)
            different = self.verify(source_sha="d" * 40)
            with self.assertRaisesRegex(
                R2S3PrecheckError, "R2_S3_RECEIPT_OUTPUT_EXISTS"
            ):
                write_r2_origin_receipt(target, different)

    def test_secret_sanitizer_covers_every_diagnostic_shape_before_output(self):
        values = environment()
        diagnostic = {
            "environment": values,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={ACCESS}, Signature={SIGNATURE}",
            "headers": {
                "Cookie": f"session={SESSION}",
                "Set-Cookie": f"token={SECRET}",
                "X-Amz-Security-Token": SESSION,
            },
            "request": (
                "Authorization: AWS4-HMAC-SHA256 "
                f"Credential={ACCESS}/20260826/auto/s3/aws4_request, "
                f"SignedHeaders=host;x-amz-date, Signature={SIGNATURE}\n"
                "https://example.invalid/key?"
                f"X-Amz-Credential={ACCESS}&X-Amz-Signature={SIGNATURE}&"
                f"X-Amz-Security-Token={SESSION}&AWSAccessKeyId={ACCESS}"
            ),
            "sdk_credential_repr": (
                f"Credentials(access_key={ACCESS!r}, secret_key={SECRET!r}, "
                f"session_token={SESSION!r})"
            ),
            "exception": FakeClientError(
                "SignatureDoesNotMatch", 403, f"{ACCESS} {SECRET} {SIGNATURE}"
            ),
            "subprocess_stderr": f"debug {ACCESS} {SECRET} {SESSION} {SIGNATURE}",
            "http_response_body": f"error {ACCESS} {SECRET} {SIGNATURE}",
            "traceback": f"trace {ACCESS} {SECRET} {SESSION} {SIGNATURE}",
            "stdout": f"debug {ACCESS} {SECRET} {SESSION} {SIGNATURE}",
            "debug_logger": f"Authorization=Bearer {SECRET}",
        }
        encoded = json.dumps(
            sanitize_r2_diagnostic(diagnostic, environment=values),
            ensure_ascii=False,
            sort_keys=True,
        )
        for sentinel in (ACCESS, SECRET, SESSION, SIGNATURE):
            self.assertEqual(encoded.count(sentinel), 0)
        self.assertNotIn("ambient-profile", encoded)
        self.assertIn("[REDACTED]", encoded)

    def test_cli_error_output_is_sanitized_and_has_no_traceback(self):
        diagnostic = {
            "Authorization": f"Bearer {SECRET}",
            "signed_url": f"https://example.invalid/?X-Amz-Signature={SIGNATURE}",
            "sdk": f"Credentials(access_key={ACCESS!r}, secret_key={SECRET!r})",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict("os.environ", environment(), clear=True), mock.patch(
            "release.cli.verify_rc14_r2_origin_from_environment",
            side_effect=R2S3PrecheckError(
                "R2_S3_AUTHENTICATION_FAILED", safe_diagnostic=diagnostic
            ),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cli.main(
                [
                    "verify-rc14-r2-origin-empty",
                    "--auth-method",
                    R2_AUTH_METHOD_ARGUMENT,
                    "--expected-source-sha",
                    SHA,
                    "--expected-source-tree",
                    TREE,
                    "--output",
                    "unused-receipt.json",
                ]
            )
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("Traceback", combined)
        for sentinel in (ACCESS, SECRET, SESSION, SIGNATURE):
            self.assertEqual(combined.count(sentinel), 0)
        self.assertEqual(
            json.loads(stderr.getvalue())["code"], "R2_S3_AUTHENTICATION_FAILED"
        )


if __name__ == "__main__":
    unittest.main()
