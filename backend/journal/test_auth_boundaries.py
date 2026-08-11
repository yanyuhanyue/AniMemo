from inspect import signature
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.response import Response

from .anti_abuse import (
    AntiAbuseChallenge,
    challenge_from_payload,
    verify_anti_abuse_challenge,
)
from . import auth_tokens
from .web_auth_adapter import access_token_from_request, clear_refresh_cookie, no_store, set_refresh_cookie


class AntiAbuseContractTests(SimpleTestCase):
    def test_canonical_challenge_precedes_the_legacy_turnstile_alias(self):
        challenge = challenge_from_payload({
            "challenge": {"provider": "turnstile", "token": "canonical"},
            "cf-turnstile-response": "legacy",
        })

        self.assertEqual(challenge, AntiAbuseChallenge(provider="turnstile", token="canonical"))

    def test_legacy_turnstile_field_remains_compatible(self):
        self.assertEqual(
            challenge_from_payload({"cf-turnstile-response": "legacy"}),
            AntiAbuseChallenge(provider="turnstile", token="legacy"),
        )

    def test_invalid_canonical_challenge_does_not_fall_back_to_legacy_field(self):
        self.assertIsNone(challenge_from_payload({
            "challenge": {"provider": "turnstile", "token": ""},
            "cf-turnstile-response": "legacy",
        }))

    @patch("journal.anti_abuse.verify_turnstile", return_value=True)
    def test_turnstile_adapter_receives_only_provider_data(self, verifier):
        challenge = AntiAbuseChallenge(provider="turnstile", token="proof")

        self.assertTrue(verify_anti_abuse_challenge(challenge, remote_ip="203.0.113.10"))
        verifier.assert_called_once_with("proof", remote_ip="203.0.113.10")

    @patch("journal.anti_abuse.verify_turnstile", return_value=True)
    def test_missing_challenge_is_delegated_to_the_default_provider_policy(self, verifier):
        self.assertTrue(verify_anti_abuse_challenge(None, remote_ip="203.0.113.10"))
        verifier.assert_called_once_with("", remote_ip="203.0.113.10")

    @patch("journal.anti_abuse.verify_turnstile")
    def test_unknown_provider_fails_closed(self, verifier):
        challenge = AntiAbuseChallenge(provider="unknown", token="proof")

        self.assertFalse(verify_anti_abuse_challenge(challenge))
        verifier.assert_not_called()


class WebAuthAdapterContractTests(SimpleTestCase):
    def test_token_core_does_not_export_cookie_or_http_response_adapters(self):
        for name in ("set_refresh_cookie", "clear_refresh_cookie", "refresh_cookie_options", "no_store"):
            self.assertFalse(hasattr(auth_tokens, name), name)

    def test_token_core_accepts_credentials_not_http_requests(self):
        self.assertNotIn("request", signature(auth_tokens.rotate_refresh).parameters)
        self.assertNotIn("request", signature(auth_tokens.revoke_access_token).parameters)

    def test_web_adapter_extracts_bearer_access_credentials(self):
        request = type("Request", (), {"META": {"HTTP_AUTHORIZATION": "Bearer access-token"}})()

        self.assertEqual(access_token_from_request(request), "access-token")

    @override_settings(
        REFRESH_COOKIE_NAME="refresh",
        REFRESH_COOKIE_PATH="/api/",
        REFRESH_COOKIE_DOMAIN=None,
        REFRESH_COOKIE_SAMESITE="Lax",
        REFRESH_COOKIE_SECURE=True,
        SIMPLE_JWT={"REFRESH_TOKEN_LIFETIME": __import__("datetime").timedelta(days=1)},
    )
    def test_web_adapter_owns_cookie_and_no_store_semantics(self):
        response = no_store(set_refresh_cookie(Response({}), "secret"))

        self.assertTrue(response.cookies["refresh"]["httponly"])
        self.assertTrue(response.cookies["refresh"]["secure"])
        self.assertEqual(response.cookies["refresh"]["path"], "/api/")
        self.assertEqual(response["Cache-Control"], "no-store")

        clear_refresh_cookie(response)
        self.assertEqual(response.cookies["refresh"]["max-age"], 0)
