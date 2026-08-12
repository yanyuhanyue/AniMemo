from __future__ import annotations

import json
import unittest

from updater.redaction import redact


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
        nested_json = json.dumps({"clientSecret": "nested-secret", "request_id": "req-19"})
        payload = {
            "username": "animeadmin",
            "access_token": "access-secret",
            "database": {"password": "database-secret", "host": "db.internal"},
            "headers": {"Authorization": "Bearer header-secret"},
            "payload": nested_json,
        }

        structured = redact(payload)
        serialized = redact(json.dumps(payload))
        embedded_json = json.dumps({"password": 'embedded "quoted" secret', "request_id": "req-20"})
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
        for diagnostic in ("sessionid=", "csrftoken=", "preference=", "Path=/api", "HttpOnly", "SameSite=Lax"):
            self.assertIn(diagnostic, value)
        self.assertIn("status=403", value)

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

    def test_cli_equals_and_separate_argument_forms_are_redacted(self):
        value = redact(
            "update --password=hunter2 --client-secret 'client secret' "
            "--aws-secret-access-key cloud-secret --token=access-secret "
            "--username animeadmin --release-id rc-2026.08"
        )
        argument_list = redact(["update", "--password", "list-secret", "--username", "animeadmin"])
        serialized_arguments = redact(json.dumps(["update", "--api-key=json-secret", "--release-id", "rc-1"]))

        self.assert_secrets_removed(value, "hunter2", "client secret", "cloud-secret", "access-secret")
        self.assert_secrets_removed(argument_list, "list-secret")
        self.assert_secrets_removed(serialized_arguments, "json-secret")
        self.assertIn("--username animeadmin", value)
        self.assertIn("--release-id rc-2026.08", value)
        self.assertIn("animeadmin", argument_list)
        self.assertIn("rc-1", serialized_arguments)

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
