from __future__ import annotations

import json
import random
import string
import tempfile
import time
import unittest
from pathlib import Path

from updater.redaction import redact
from updater.state import OperationStore


class RedactionTests(unittest.TestCase):
    def assert_secrets_removed(self, value: str, *secrets: str) -> None:
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, value)

    def test_secrets_are_removed_from_logs(self):
        value = redact(
            "Authorization: Bearer abc.def\n"
            "DB_PASSWORD=hunter2 "
            "https://user:secret@example.test "
            "GITHUB_TOKEN=ghp_example refresh_token=very-secret"
        )

        self.assert_secrets_removed(
            value,
            "abc.def",
            "hunter2",
            "secret@example",
            "ghp_example",
            "very-secret",
        )
        self.assertIn("[REDACTED]", value)

    def test_structured_and_serialized_json_are_redacted_recursively(self):
        nested_json = json.dumps(
            {"clientSecret": "nested-secret", "request_id": "req-19"}
        )
        payload = {
            "username": "animeadmin",
            "access_token": "access-secret",
            "database": {"password": "database-secret", "host": "db.internal"},
            "headers": {"Authorization": "Bearer header-secret"},
            "payload": nested_json,
        }

        structured = redact(payload)
        serialized = redact(json.dumps(payload))
        embedded_json = json.dumps(
            {"password": 'embedded "quoted" secret', "request_id": "req-20"}
        )
        embedded = redact(f"upstream body={json.dumps(embedded_json)} status=400")

        for output in (structured, serialized):
            self.assert_secrets_removed(
                output,
                "access-secret",
                "database-secret",
                "header-secret",
                "nested-secret",
            )
            self.assertIn("animeadmin", output)
            self.assertIn("db.internal", output)
            self.assertIn("req-19", output)
        self.assertNotIn("embedded", embedded)
        self.assertNotIn("quoted", embedded)
        self.assertIn("status=400", embedded)
        self.assertIn("req-20", embedded)

    def test_truncated_json_credentials_fail_closed(self):
        plain = redact('upstream={"password":"TRUNCATED_JSON_SECRET')
        escaped = redact(r"body={\"refresh_token\":\"TRUNCATED_ESCAPED_SECRET")
        trailing_escape = redact(
            'upstream={"password":"TRAILING_ESCAPE_JSON_SECRET' + "\\"
        )
        mismatched_key_quote = redact(
            'upstream={"password\':"MALFORMED_KEY_JSON_SECRET" status=400'
        )
        double_escaped = redact(
            r"body={\\\"password\\\":\\\"DOUBLE_ESCAPED_JSON_SECRET\\\"} status=422"
        )

        self.assert_secrets_removed(
            plain,
            "TRUNCATED_JSON_SECRET",
        )
        self.assert_secrets_removed(
            escaped,
            "TRUNCATED_ESCAPED_SECRET",
        )
        self.assert_secrets_removed(trailing_escape, "TRAILING_ESCAPE_JSON_SECRET")
        self.assert_secrets_removed(
            mismatched_key_quote,
            "MALFORMED_KEY_JSON_SECRET",
        )
        self.assert_secrets_removed(double_escaped, "DOUBLE_ESCAPED_JSON_SECRET")
        self.assertEqual(plain, 'upstream={"password":"[REDACTED]')
        self.assertEqual(escaped, r"body={\"refresh_token\":\"[REDACTED]")
        self.assertIn("status=400", mismatched_key_quote)
        self.assertIn("status=422", double_escaped)

    def test_cookie_headers_redact_values_but_keep_names_and_attributes(self):
        value = redact(
            "Cookie: sessionid=session-secret; csrftoken=csrf-secret; preference=compact\n"
            "Set-Cookie: refresh=refresh-secret; Path=/api; HttpOnly; Secure; SameSite=Lax\n"
            "curl -H 'Cookie: session=inline-cookie' status=403\n"
            "HTTP_COOKIE=wsgi=wsgi-cookie; preference=wide"
        )

        self.assert_secrets_removed(
            value,
            "session-secret",
            "csrf-secret",
            "compact",
            "refresh-secret",
            "inline-cookie",
            "wsgi-cookie",
            "wide",
        )
        for diagnostic in (
            "sessionid=",
            "csrftoken=",
            "preference=",
            "Path=/api",
            "HttpOnly",
            "SameSite=Lax",
        ):
            self.assertIn(diagnostic, value)
        self.assertIn("status=403", value)

    def test_crlf_cookie_headers_are_redacted(self):
        value = redact(
            "Cookie: sid=CRLF_COOKIE_SECRET; preference=compact\r\n"
            "Set-Cookie: refresh=CRLF_SET_COOKIE_SECRET; Path=/; HttpOnly\r\n"
            "status=401 request_id=req-crlf-cookie"
        )

        self.assert_secrets_removed(
            value,
            "CRLF_COOKIE_SECRET",
            "compact",
            "CRLF_SET_COOKIE_SECRET",
        )
        self.assertIn("sid=", value)
        self.assertIn("refresh=", value)
        self.assertIn("Path=/", value)
        self.assertIn("status=401", value)

    def test_combined_structured_and_scalar_cookie_values_are_all_redacted(self):
        combined = redact(
            "Set-Cookie: sid=FIRST_COOKIE_SECRET; Path=/, "
            "refresh=SECOND_COOKIE_SECRET; HttpOnly; SameSite=Lax\n"
            "status=401 request_id=req-cookie"
        )
        structured = redact(
            {
                "cookies": {
                    "session": "STRUCTURED_SESSION_SECRET",
                    "refresh": "STRUCTURED_REFRESH_SECRET",
                    "preference": "compact",
                },
                "request_id": "req-cookie-jar",
            }
        )
        scalar = redact(
            "HTTP_COOKIE=RAW_COOKIE_SECRET status=403 operation_id=op-cookie"
        )

        self.assert_secrets_removed(
            combined,
            "FIRST_COOKIE_SECRET",
            "SECOND_COOKIE_SECRET",
        )
        for diagnostic in (
            "sid=",
            "refresh=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "status=401",
            "request_id=req-cookie",
        ):
            self.assertIn(diagnostic, combined)
        self.assert_secrets_removed(
            structured,
            "STRUCTURED_SESSION_SECRET",
            "STRUCTURED_REFRESH_SECRET",
            "compact",
        )
        self.assertIn("req-cookie-jar", structured)
        self.assert_secrets_removed(scalar, "RAW_COOKIE_SECRET")
        self.assertIn("status=403", scalar)
        self.assertIn("operation_id=op-cookie", scalar)

    def test_combined_set_cookie_expires_and_cookie_objects_keep_safe_metadata(self):
        combined = redact(
            "Set-Cookie: session=EXPIRES_COOKIE_SECRET; "
            "Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/, "
            "refresh=COMBINED_COOKIE_SECRET; HttpOnly\n"
            "status=401 request_id=req-cookie-expires"
        )
        structured = redact(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "OBJECT_COOKIE_SECRET",
                        "domain": "example.test",
                        "path": "/api",
                    }
                ],
                "operation_id": "op-cookie-object",
            }
        )

        self.assert_secrets_removed(
            combined,
            "EXPIRES_COOKIE_SECRET",
            "COMBINED_COOKIE_SECRET",
        )
        for diagnostic in (
            "session=",
            "refresh=",
            "Expires=Wed, 21 Oct 2026 07:28:00 GMT",
            "Path=/",
            "HttpOnly",
            "status=401",
            "request_id=req-cookie-expires",
        ):
            self.assertIn(diagnostic, combined)
        self.assert_secrets_removed(structured, "OBJECT_COOKIE_SECRET")
        for diagnostic in ("session", "example.test", "/api", "op-cookie-object"):
            self.assertIn(diagnostic, structured)

    def test_malformed_and_ambiguous_cookie_containers_fail_closed(self):
        malformed = redact(
            "Cookie: first=FIRST_COOKIE_SECRET; DANGLING_COOKIE_SECRET, "
            "second=SECOND_COOKIE_SECRET"
        )
        ambiguous = redact(
            {
                "cookies": {
                    "value": "COOKIE_NAMED_VALUE_SECRET",
                    "session": "AMBIGUOUS_SESSION_SECRET",
                },
                "request_id": "req-ambiguous-cookie",
            }
        )
        inline = redact(
            "HTTP_COOKIE=sid=INLINE_COOKIE_SECRET; path=PATH_COOKIE_SECRET "
            "status=403 operation_id=op-inline-cookie"
        )
        line_with_context = redact(
            "Cookie: sid=LINE_COOKIE_SECRET status=429 request_id=req-cookie-line"
        )

        self.assert_secrets_removed(
            malformed,
            "FIRST_COOKIE_SECRET",
            "DANGLING_COOKIE_SECRET",
            "SECOND_COOKIE_SECRET",
        )
        self.assertIn("first=", malformed)
        self.assertIn("second=", malformed)
        self.assert_secrets_removed(
            ambiguous,
            "COOKIE_NAMED_VALUE_SECRET",
            "AMBIGUOUS_SESSION_SECRET",
        )
        self.assertIn("req-ambiguous-cookie", ambiguous)
        self.assert_secrets_removed(
            inline,
            "INLINE_COOKIE_SECRET",
            "PATH_COOKIE_SECRET",
        )
        self.assertIn("status=403", inline)
        self.assertIn("operation_id=op-inline-cookie", inline)
        self.assert_secrets_removed(line_with_context, "LINE_COOKIE_SECRET")
        self.assertIn("status=429", line_with_context)
        self.assertIn("request_id=req-cookie-line", line_with_context)

    def test_top_level_request_and_header_pair_cookie_shapes_are_redacted(self):
        top_level = redact([{"name": "sid", "value": "TOP_LEVEL_COOKIE_SECRET"}])
        request_cookies = redact(
            {
                "requestCookies": [{"name": "sid", "value": "REQUEST_COOKIE_SECRET"}],
                "request_id": "req-cookie-shapes",
            }
        )
        header_pair = redact([("Cookie", "sid=HEADER_PAIR_COOKIE_SECRET")])

        self.assert_secrets_removed(top_level, "TOP_LEVEL_COOKIE_SECRET")
        self.assert_secrets_removed(request_cookies, "REQUEST_COOKIE_SECRET")
        self.assert_secrets_removed(header_pair, "HEADER_PAIR_COOKIE_SECRET")
        self.assertIn("req-cookie-shapes", request_cookies)
        self.assertIn("sid=", header_pair)

    def test_authorization_variants_are_redacted_across_multiline_output(self):
        value = redact(
            "Authorization: bearer bearer-secret\n"
            "authorization: Basic basic-secret\n"
            "Proxy-Authorization: Token proxy-secret\n"
            'Authorization: Digest username="bob", response="digest-secret"\n'
            "request failed Authorization=ApiKey inline-secret status=401\n"
            'curl -H "Authorization: Bearer curl-secret" method=GET\n'
            "HTTP_AUTHORIZATION=Bearer wsgi-secret status=401\n"
            'request Authorization=Digest response="inline-digest" status=403 request_id=req-auth\n'
            "operation_id=op-42"
        )

        self.assert_secrets_removed(
            value,
            "bearer-secret",
            "basic-secret",
            "proxy-secret",
            "digest-secret",
            "inline-secret",
            "curl-secret",
            "wsgi-secret",
            "inline-digest",
        )
        self.assertIn("status=401", value)
        self.assertIn("status=403", value)
        self.assertIn("request_id=req-auth", value)
        self.assertIn("method=GET", value)
        self.assertIn("operation_id=op-42", value)

    def test_arbitrary_and_folded_authorization_credentials_are_fully_redacted(self):
        value = redact(
            'request Authorization=Hawk id="client", mac="HAWK_SECRET" '
            "status=401 request_id=req-hawk\n"
            'request Authorization=Signature keyId="demo",signature="SIGNATURE_SECRET" '
            "method=GET operation_id=op-signature\n"
            "Authorization: Custom custom-prefix\n"
            " FOLDED_AUTHORIZATION_SECRET\n"
            'request Authorization=Digest username="bob", '
            'path="AUTHORIZATION_PATH_SECRET", response="AUTHORIZATION_RESPONSE_SECRET" '
            "status=402 request_id=req-digest-boundary\n"
            "Authorization: Custom INLINE_AUTHORIZATION_SECRET "
            "status=429 request_id=req-inline-header\n"
            "  traceback_frame=updater.agent\n"
            "status=403 request_id=req-folded"
        )

        self.assert_secrets_removed(
            value,
            "HAWK_SECRET",
            "SIGNATURE_SECRET",
            "custom-prefix",
            "FOLDED_AUTHORIZATION_SECRET",
            "AUTHORIZATION_PATH_SECRET",
            "AUTHORIZATION_RESPONSE_SECRET",
            "INLINE_AUTHORIZATION_SECRET",
        )
        for diagnostic in (
            "Hawk [REDACTED]",
            "Signature [REDACTED]",
            "status=401",
            "request_id=req-hawk",
            "method=GET",
            "operation_id=op-signature",
            "status=402",
            "request_id=req-digest-boundary",
            "status=429",
            "request_id=req-inline-header",
            "traceback_frame=updater.agent",
            "status=403",
            "request_id=req-folded",
        ):
            self.assertIn(diagnostic, value)

    def test_prefixed_crlf_authorization_headers_redact_folded_lines(self):
        value = redact(
            "> Authorization: Custom PREFIXED_AUTH_SECRET\r\n"
            " FOLDED_PREFIXED_AUTH_SECRET\r\n"
            "headers: Authorization: Other HEADER_AUTH_SECRET\r\n"
            "\tFOLDED_HEADER_AUTH_SECRET\r\n"
            "status=401 request_id=req-prefixed-auth"
        )

        self.assert_secrets_removed(
            value,
            "PREFIXED_AUTH_SECRET",
            "FOLDED_PREFIXED_AUTH_SECRET",
            "HEADER_AUTH_SECRET",
            "FOLDED_HEADER_AUTH_SECRET",
        )
        self.assertIn("status=401", value)
        self.assertIn("request_id=req-prefixed-auth", value)

    def test_cli_equals_and_separate_argument_forms_are_redacted(self):
        value = redact(
            "update --password=hunter2 --client-secret 'client secret' "
            "--aws-secret-access-key cloud-secret --token=access-secret "
            "--username animeadmin --release-id rc-2026.08"
        )
        argument_list = redact(
            ["update", "--password", "list-secret", "--username", "animeadmin"]
        )
        serialized_arguments = redact(
            json.dumps(["update", "--api-key=json-secret", "--release-id", "rc-1"])
        )

        self.assert_secrets_removed(
            value, "hunter2", "client secret", "cloud-secret", "access-secret"
        )
        self.assert_secrets_removed(argument_list, "list-secret")
        self.assert_secrets_removed(serialized_arguments, "json-secret")
        self.assertIn("--username animeadmin", value)
        self.assertIn("--release-id rc-2026.08", value)
        self.assertIn("animeadmin", argument_list)
        self.assertIn("rc-1", serialized_arguments)

    def test_unterminated_quoted_cli_secret_fails_closed(self):
        value = redact(
            'command --password "MULTI WORD SECRET status=failed request_id=req-truncated'
        )
        trailing_escape = redact(
            'command --client-secret "TRAILING_ESCAPE_CLI_SECRET' + "\\"
        )

        self.assert_secrets_removed(value, "MULTI", "WORD", "SECRET")
        self.assert_secrets_removed(trailing_escape, "TRAILING_ESCAPE_CLI_SECRET")
        self.assertEqual(value, "command --password [REDACTED]")

    def test_cli_secret_punctuation_and_authorization_are_idempotent(self):
        value = redact(
            "--password=CLI_COMMA_HEAD,CLI_COMMA_TAIL "
            "--token CLI_SEMI_HEAD;CLI_SEMI_TAIL "
            "--Authorization=Bearer CLI_AUTH_SECRET "
            "--release-id rc-1"
        )

        self.assert_secrets_removed(
            value,
            "CLI_COMMA_HEAD",
            "CLI_COMMA_TAIL",
            "CLI_SEMI_HEAD",
            "CLI_SEMI_TAIL",
            "CLI_AUTH_SECRET",
        )
        self.assertIn("--release-id rc-1", value)
        self.assertEqual(redact(value), value)

    def test_bare_secret_punctuation_and_authorization_pairs_fail_closed(self):
        value = redact(
            "failure password=BARE_COMMA_HEAD,BARE_COMMA_TAIL status=500\n"
            "failure api_token=BARE_SEMI_HEAD;BARE_SEMI_TAIL request_id=req-bare\n"
            "DATABASE_URL=postgresql://user:DB_HEAD;DB_TAIL@db.example.test/app "
            "operation_id=op-bare"
        )
        authorization_pair = redact(
            [("Authorization", "Bearer HEADER_PAIR_SECRET")]
        )
        sensitive_pair = redact([("api_token", "STRUCTURED_PAIR_SECRET")])

        self.assert_secrets_removed(
            value,
            "BARE_COMMA_HEAD",
            "BARE_COMMA_TAIL",
            "BARE_SEMI_HEAD",
            "BARE_SEMI_TAIL",
            "DB_HEAD",
            "DB_TAIL",
        )
        self.assert_secrets_removed(authorization_pair, "HEADER_PAIR_SECRET")
        self.assert_secrets_removed(sensitive_pair, "STRUCTURED_PAIR_SECRET")
        self.assertIn("status=500", value)
        self.assertIn("request_id=req-bare", value)
        self.assertIn("operation_id=op-bare", value)
        self.assertIn("Bearer [REDACTED]", authorization_pair)
        self.assertIn("[REDACTED]", sensitive_pair)
        self.assertEqual(redact(value), value)
        self.assertEqual(redact(authorization_pair), authorization_pair)
        self.assertEqual(redact(sensitive_pair), sensitive_pair)

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            operation = store.create("apply_update", {"version": "v1.0.1"})
            store.transition(
                operation["id"],
                "preflight",
                detail=(
                    "stderr: password=PERSISTED_HEAD,PERSISTED_TAIL "
                    "status=500"
                ),
            )
            persisted = store.get(operation["id"])["events"][-1]["detail"]

        self.assert_secrets_removed(
            persisted,
            "PERSISTED_HEAD",
            "PERSISTED_TAIL",
        )
        self.assertIn("status=500", persisted)

    def test_bare_secret_structure_characters_do_not_truncate_redaction(self):
        cases = (
            (
                "password=HEAD}TAIL_BRACE_SECRET status=500",
                "TAIL_BRACE_SECRET",
            ),
            (
                "api_token=HEAD]TAIL_BRACKET_SECRET request_id=req-bracket",
                "TAIL_BRACKET_SECRET",
            ),
            (
                "DATABASE_URL=postgres://u:HEAD&TAIL_AMP_SECRET@db/app "
                "operation_id=op-amp",
                "TAIL_AMP_SECRET",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            for diagnostic, secret in cases:
                with self.subTest(secret=secret):
                    output = redact(diagnostic)
                    operation = store.create("apply_update", {"version": "v1.0.1"})
                    stored = store.transition(
                        operation["id"], "preflight", detail=diagnostic
                    )
                    persisted = stored["events"][-1]["detail"]
                    disk = (
                        Path(directory)
                        / "operations"
                        / f"{operation['id']}.json"
                    ).read_text()

                    self.assert_secrets_removed(output, secret)
                    self.assert_secrets_removed(persisted, secret)
                    self.assert_secrets_removed(disk, secret)
                    self.assertEqual(redact(output), output)

    def test_untrusted_redacted_marker_suffix_is_not_treated_as_safe(self):
        cases = (
            (
                "password=[REDACTED], fake=TAIL_FAKE_MARKER_SECRET",
                "TAIL_FAKE_MARKER_SECRET",
            ),
            (
                "api_token=[REDACTED]; fake=TAIL_FAKE_SEMI_SECRET",
                "TAIL_FAKE_SEMI_SECRET",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            for diagnostic, secret in cases:
                with self.subTest(secret=secret):
                    output = redact(diagnostic)
                    operation = store.create("apply_update", {"version": "v1.0.1"})
                    stored = store.transition(
                        operation["id"], "preflight", detail=diagnostic
                    )
                    persisted = stored["events"][-1]["detail"]
                    disk = (
                        Path(directory)
                        / "operations"
                        / f"{operation['id']}.json"
                    ).read_text()

                    self.assert_secrets_removed(output, secret)
                    self.assert_secrets_removed(persisted, secret)
                    self.assert_secrets_removed(disk, secret)
                    self.assertEqual(redact(output), output)

    def test_syntax_specific_redaction_does_not_hide_nested_credentials(self):
        cases = (
            (
                "https://example.test/?note=password=URL_NESTED_SECRET"
                "&trace_id=trace-7",
                "URL_NESTED_SECRET",
            ),
            (
                "https://example.test/?token=[REDACTED]"
                "&note=password=URL_MARKER_NESTED_SECRET&trace_id=trace-8",
                "URL_MARKER_NESTED_SECRET",
            ),
            (
                "failure password=HEAD&trace_id=TAIL_QUERY_BOUNDARY_SECRET",
                "TAIL_QUERY_BOUNDARY_SECRET",
            ),
            (
                "Set-Cookie: sid=COOKIE_SECRET; "
                "Path=password=COOKIE_PATH_SECRET; HttpOnly status=401",
                "COOKIE_PATH_SECRET",
            ),
            (
                "Set-Cookie: sid=[REDACTED]; "
                "Path=password=COOKIE_MARKER_PATH_SECRET; HttpOnly status=401",
                "COOKIE_MARKER_PATH_SECRET",
            ),
            (
                "Set-Cookie: sid=COOKIE_SECRET; "
                "Comment=api_token=COOKIE_COMMENT_SECRET; Secure status=401",
                "COOKIE_COMMENT_SECRET",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            for diagnostic, secret in cases:
                with self.subTest(secret=secret):
                    output = redact(diagnostic)
                    operation = store.create("apply_update", {"version": "v1.0.1"})
                    stored = store.transition(
                        operation["id"], "preflight", detail=diagnostic
                    )
                    persisted = stored["events"][-1]["detail"]
                    disk = (
                        Path(directory)
                        / "operations"
                        / f"{operation['id']}.json"
                    ).read_text()

                    self.assert_secrets_removed(output, secret)
                    self.assert_secrets_removed(persisted, secret)
                    self.assert_secrets_removed(disk, secret)
                    self.assertEqual(redact(output), output)

    def test_authentication_credential_key_variants_are_redacted(self):
        keys = (
            "setup_code",
            "bootstrap_code",
            "recovery_code",
            "otp",
            "totp",
            "passphrase",
            "private_key_passphrase",
            "client_assertion",
            "refresh_credential",
            "access_credential",
            "session_key",
        )

        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            for index, key in enumerate(keys):
                secret = f"CREDENTIAL_CANARY_{index:02d}_SECRET_VALUE"
                diagnostic = f"failure {key}={secret} status=500"
                with self.subTest(key=key):
                    text_output = redact(diagnostic)
                    structured_output = redact({key: secret, "status": 500})
                    operation = store.create("apply_update", {"version": "v1.0.1"})
                    stored = store.transition(
                        operation["id"], "preflight", detail=diagnostic
                    )
                    persisted = stored["events"][-1]["detail"]
                    disk = (
                        Path(directory)
                        / "operations"
                        / f"{operation['id']}.json"
                    ).read_text()

                    self.assert_secrets_removed(text_output, secret)
                    self.assert_secrets_removed(structured_output, secret)
                    self.assert_secrets_removed(persisted, secret)
                    self.assert_secrets_removed(disk, secret)
                    self.assertIn("status=500", text_output)

    def test_key_value_logs_and_common_cloud_credentials_are_redacted(self):
        value = redact(
            "db_password: database-secret\n"
            "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\n"
            "R2_SECRET_ACCESS_KEY: r2-secret\n"
            'AZURE_CLIENT_SECRET="azure secret"\n'
            "GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/google.json\n"
            "SENTRY_DSN=https://public:dsn-secret@sentry.example/1\n"
            "password: correct horse battery staple status=401 request_id=req-11\n"
            "PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n"
            "MII-private-material\n"
            "-----END PRIVATE KEY-----\n"
            "token_count=7 token_type: access"
        )

        self.assert_secrets_removed(
            value,
            "database-secret",
            "AKIAEXAMPLE",
            "r2-secret",
            "azure secret",
            "/run/secrets/google.json",
            "dsn-secret",
            "correct horse battery staple",
            "MII-private-material",
        )
        self.assertIn("token_count=7", value)
        self.assertIn("token_type: access", value)
        self.assertIn("status=401", value)
        self.assertIn("request_id=req-11", value)

    def test_truncated_private_key_blocks_are_redacted_to_end_of_diagnostic(self):
        value = redact(
            "PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n"
            "truncated-private-material\n"
            "still-private"
        )

        self.assert_secrets_removed(
            value, "truncated-private-material", "still-private"
        )
        self.assertIn("[REDACTED]", value)

    def test_pgp_and_truncated_begin_private_key_blocks_fail_closed(self):
        pgp = redact(
            "PRIVATE_KEY=-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
            "PGP_PRIVATE_MATERIAL\n"
            "-----END PGP PRIVATE KEY BLOCK-----\n"
            "status=failed request_id=req-pgp"
        )
        truncated_begin = redact(
            "PRIVATE_KEY=-----BEGIN PRIVATE KEY----\n"
            "TRUNCATED_BEGIN_PRIVATE_MATERIAL\n"
            "still-private"
        )
        whitespace_begin = redact(
            "diagnostic -----BEGIN OPENSSH PRIVATE KEY---   \n"
            "WHITESPACE_BEGIN_PRIVATE_MATERIAL"
        )

        self.assert_secrets_removed(pgp, "PGP_PRIVATE_MATERIAL")
        self.assertIn("status=failed", pgp)
        self.assertIn("request_id=req-pgp", pgp)
        self.assert_secrets_removed(
            truncated_begin,
            "TRUNCATED_BEGIN_PRIVATE_MATERIAL",
            "still-private",
        )
        self.assert_secrets_removed(
            whitespace_begin,
            "WHITESPACE_BEGIN_PRIVATE_MATERIAL",
        )

    def test_private_key_labels_truncated_inside_words_fail_closed(self):
        pgp = redact("-----BEGIN PGP PRIVATE KEY BLOC\nPGP_LABEL_FRAGMENT_SECRET\n")
        encrypted = redact(
            "-----BEGIN ENCRYPTED PRIVATE KE\r\nPRIVATE_LABEL_FRAGMENT_SECRET\r\n"
        )

        self.assert_secrets_removed(pgp, "PGP_LABEL_FRAGMENT_SECRET")
        self.assert_secrets_removed(encrypted, "PRIVATE_LABEL_FRAGMENT_SECRET")

    def test_excessive_nested_depth_fails_closed(self):
        nested: object = {"password": "deep-secret"}
        for _ in range(12):
            nested = {"level": nested}

        structured = redact(nested)
        serialized = redact(json.dumps(nested))

        self.assert_secrets_removed(structured, "deep-secret")
        self.assert_secrets_removed(serialized, "deep-secret")
        self.assertIn("[REDACTED]", structured)
        self.assertIn("[REDACTED]", serialized)

    def test_parsed_json_exceeding_depth_is_replaced_as_a_whole(self):
        value = '{"password":"DEPTH_LIMIT_SECRET"}'
        for _ in range(12):
            value = f"[{value}]"

        output = redact(value)

        self.assertEqual(output, "[REDACTED]")
        self.assert_secrets_removed(output, "DEPTH_LIMIT_SECRET")

    def test_json_parser_recursion_error_fails_closed(self):
        value = "[" * 5_000 + '{"password":"DEEP_JSON_SECRET"}' + "]" * 5_000

        output = redact(value)

        self.assertEqual(output, "[REDACTED]")
        self.assert_secrets_removed(output, "DEEP_JSON_SECRET")
        with tempfile.TemporaryDirectory() as directory:
            store = OperationStore(Path(directory))
            operation = store.create("apply_update", {"version": "v1.0.1"})
            stored = store.transition(operation["id"], "preflight", detail=value)

        self.assertEqual(stored["events"][-1]["detail"], "[REDACTED]")

    def test_deterministic_adversarial_secret_formats_do_not_leak(self):
        generator = random.Random(0xA11CE)
        alphabet = string.ascii_letters + string.digits + "._~+/=-"
        templates = (
            'upstream={{"password":"{secret}',
            r"body={{\"refresh_token\":\"{secret}",
            'command --client-secret "{secret}',
            "Authorization: Scheme{index} {secret}",
            "Cookie: sid={secret}; preference={other}",
            "Set-Cookie: sid={secret}; Path=/, refresh={other}; HttpOnly",
            "PRIVATE_KEY=-----BEGIN PGP PRIVATE KEY BLOCK----\n{secret}",
        )

        for index in range(64):
            secret = "FUZZ_SECRET_" + "".join(
                generator.choice(alphabet) for _ in range(48)
            )
            other = "FUZZ_COOKIE_" + "".join(
                generator.choice(alphabet) for _ in range(48)
            )
            for template in templates:
                with self.subTest(index=index, template=template[:20]):
                    output = redact(
                        template.format(index=index, secret=secret, other=other)
                    )
                    secrets = (secret, other) if "{other}" in template else (secret,)
                    self.assert_secrets_removed(output, *secrets)
                    self.assertEqual(redact(output), output)

    def test_one_mib_inputs_have_a_bounded_scan_cost(self):
        size = 1024 * 1024
        cases = {
            "benign": "a" * size,
            "adversarial_truncated": 'password="' + "\\" * (size - 10),
            "adversarial_many_lines": (
                "\n" * (size - len("Authorization: Custom MANY_LINES_SECRET"))
                + "Authorization: Custom MANY_LINES_SECRET"
            ),
            "adversarial_many_headers": (
                "Authorization: Scheme MANY_HEADER_SECRET\n"
                * ((size // len("Authorization: Scheme MANY_HEADER_SECRET\n")) + 1)
            )[:size],
        }

        for name, value in cases.items():
            with self.subTest(name=name):
                started = time.monotonic()
                output = redact(value)
                elapsed = time.monotonic() - started

                if name == "benign":
                    self.assertEqual(output, value)
                elif name == "adversarial_truncated":
                    self.assertNotIn("\\" * 1_000, output)
                elif name == "adversarial_many_lines":
                    self.assertNotIn("MANY_LINES_SECRET", output)
                else:
                    self.assertNotIn("MANY_HEADER_SECRET", output)
                self.assertLess(elapsed, 5.0)

    def test_url_credentials_and_secret_query_parameters_are_redacted(self):
        value = redact(
            "postgresql://dbuser:db-password@db.internal:5432/animemo "
            "rediss://:redis-password@redis.internal/0 "
            "https://objects.example/file?X-Amz-Signature=signed-secret&trace_id=trace-7 "
            "https://blob.example/file?sig=azure-sas-secret&api%5Fkey=encoded-secret&operation_id=op-8 "
            "https://example.test/users/animeadmin?request_id=req-8"
        )

        self.assert_secrets_removed(
            value,
            "db-password",
            "redis-password",
            "signed-secret",
            "azure-sas-secret",
            "encoded-secret",
        )
        for diagnostic in (
            "dbuser:",
            "db.internal:5432",
            "redis.internal",
            "trace_id=trace-7",
            "operation_id=op-8",
            "request_id=req-8",
        ):
            self.assertIn(diagnostic, value)

    def test_harmless_identifiers_are_not_over_redacted(self):
        value = redact(
            "username=animeadmin user_id=17 entry_id=42 request_id=req-9 "
            "client_id=client-public token_count=12 token_type=access "
            "public_key_id=key-3 artifact_signature=sha256-public "
            "commit=67997c7 version=1.0.0 channel=rc "
            "https://reader@example.test/profile"
        )

        self.assertNotIn("[REDACTED]", value)
        for diagnostic in (
            "username=animeadmin",
            "user_id=17",
            "entry_id=42",
            "request_id=req-9",
            "client_id=client-public",
            "token_count=12",
            "token_type=access",
            "public_key_id=key-3",
            "artifact_signature=sha256-public",
            "commit=67997c7",
            "version=1.0.0",
            "channel=rc",
            "https://reader@example.test/profile",
        ):
            self.assertIn(diagnostic, value)


if __name__ == "__main__":
    unittest.main()
