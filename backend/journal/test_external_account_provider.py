import json
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests
from django.test import SimpleTestCase, override_settings

from journal.external_accounts.errors import ExternalAccountError
from journal.external_accounts.providers.bangumi import BangumiAccountProvider


def response(payload, *, status_code=200, content=None, headers=None):
    result = Mock()
    result.status_code = status_code
    content = json.dumps(payload).encode("utf-8") if content is None else content
    result.headers = headers or {}
    result.raise_for_status.return_value = None
    result.iter_content.return_value = [content]
    return result


class BangumiAccountProviderTests(SimpleTestCase):
    def setUp(self):
        self.provider = BangumiAccountProvider()

    @override_settings(
        BANGUMI_OAUTH_CLIENT_ID="client-id",
        BANGUMI_OAUTH_CLIENT_SECRET="client-secret",
        BANGUMI_OAUTH_REDIRECT_URI="https://example.test/api/external-accounts/bangumi/callback/",
    )
    def test_authorization_url_uses_verified_official_contract(self):
        parsed = urlparse(self.provider.authorization_url("random-state"))
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://bgm.tv/oauth/authorize")
        self.assertEqual(parse_qs(parsed.query), {
            "client_id": ["client-id"],
            "response_type": ["code"],
            "redirect_uri": ["https://example.test/api/external-accounts/bangumi/callback/"],
            "state": ["random-state"],
        })

    @patch("journal.external_accounts.providers.bangumi.requests.request")
    def test_verify_account_uses_bearer_and_normalizes_stable_identity(self, request):
        request.return_value = response({
            "id": 123,
            "username": "sai",
            "nickname": "Sai",
            "avatar": {"large": "https://lain.bgm.tv/pic/user/l/1.jpg"},
        })
        profile = self.provider.verify_account("token-value")
        self.assertEqual(profile["external_user_id"], "123")
        self.assertEqual(profile["external_username"], "sai")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer token-value")
        self.assertEqual(request.call_args.args[:2], ("get", "https://api.bgm.tv/v0/me"))

    @patch("journal.external_accounts.providers.bangumi.requests.request")
    def test_invalid_token_is_mapped_without_echoing_token(self, request):
        request.return_value = response({}, status_code=401)
        with self.assertRaises(ExternalAccountError) as caught:
            self.provider.verify_account("do-not-leak-token")
        self.assertEqual(caught.exception.detail["code"], "external_account_token_invalid")
        self.assertNotIn("do-not-leak-token", str(caught.exception.detail))

    @patch("journal.external_accounts.providers.bangumi.requests.request")
    def test_collection_pagination_is_anime_only_and_bounded(self, request):
        self.assertIsNone(self.provider.normalize_collection({"subject_id": 9, "subject_type": 1, "type": 2}))
        request.side_effect = [
            response({
                "total": 2,
                "limit": 1,
                "offset": 0,
                "data": [{
                    "subject_id": 10,
                    "subject_type": 2,
                    "type": 1,
                    "rate": 0,
                    "tags": [],
                    "ep_status": 0,
                    "vol_status": 0,
                    "updated_at": "2026-01-01T00:00:00+08:00",
                    "private": False,
                    "subject": {"id": 10, "type": 2, "name": "A", "name_cn": "动画 A", "images": {}},
                }],
            }),
            response({
                "total": 2,
                "limit": 1,
                "offset": 1,
                "data": [{
                    "subject_id": 11,
                    "subject_type": 2,
                    "type": 3,
                    "rate": 9,
                    "tags": ["tag"],
                    "ep_status": 1,
                    "vol_status": 0,
                    "updated_at": "2026-01-02T00:00:00+08:00",
                    "private": False,
                    "subject": {"id": 11, "type": 2, "name": "B", "name_cn": "动画 B", "images": {}},
                }],
            }),
        ]
        rows = self.provider.get_collections("token-value", "user/name", max_items=10)
        self.assertEqual([row["external_id"] for row in rows], ["10", "11"])
        self.assertIsNone(rows[0]["remote_rating"])
        self.assertEqual(rows[1]["remote_status"], "watching")
        self.assertEqual(rows[1]["remote_rating"], 9)
        self.assertEqual(request.call_args_list[0].kwargs["params"], {"subject_type": 2, "limit": 50, "offset": 0})
        self.assertIn("user%2Fname", request.call_args_list[0].args[1])

    @patch("journal.external_accounts.providers.bangumi.requests.request")
    def test_get_retries_transient_failure_once(self, request):
        request.side_effect = [requests.Timeout("secret timeout"), response({
            "id": 1,
            "username": "retry-user",
            "nickname": "Retry",
            "avatar": {},
        })]
        with self.assertLogs("journal.external_accounts.providers.bangumi", level="WARNING") as logs:
            profile = self.provider.verify_account("token-value")
        self.assertEqual(profile["external_user_id"], "1")
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("token-value", "\n".join(logs.output))

    @patch("journal.external_accounts.providers.bangumi.requests.request")
    def test_final_timeout_is_bounded_and_token_free(self, request):
        request.side_effect = requests.Timeout("timeout detail")
        with self.assertLogs("journal.external_accounts.providers.bangumi", level="WARNING") as logs:
            with self.assertRaises(ExternalAccountError) as caught:
                self.provider.verify_account("private-token-value")
        self.assertEqual(caught.exception.detail["code"], "provider_unavailable")
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("private-token-value", "\n".join(logs.output))

    @patch("journal.external_accounts.providers.bangumi.requests.request")
    def test_oversized_response_is_rejected_before_json_decode(self, request):
        request.return_value = response(
            {},
            headers={"Content-Length": str(self.provider.max_response_bytes + 1)},
        )
        with self.assertRaises(ExternalAccountError) as caught:
            self.provider.verify_account("private-token-value")
        self.assertEqual(caught.exception.detail["code"], "provider_invalid_response")
        request.return_value.iter_content.assert_not_called()

    @override_settings(
        BANGUMI_OAUTH_CLIENT_ID="client-id",
        BANGUMI_OAUTH_CLIENT_SECRET="client-secret",
        BANGUMI_OAUTH_REDIRECT_URI="https://example.test/callback",
    )
    @patch("journal.external_accounts.providers.bangumi.requests.request")
    def test_oauth_exchange_uses_server_side_secret_and_bounded_token_payload(self, request):
        request.return_value = response({
            "access_token": "access-token-value",
            "refresh_token": "refresh-token-value",
            "expires_in": 604800,
            "token_type": "Bearer",
        })
        payload = self.provider.exchange_code("short-code", "state-value")
        self.assertEqual(payload["access_token"], "access-token-value")
        sent = request.call_args.kwargs["data"]
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["client_secret"], "client-secret")
        self.assertEqual(request.call_args.args[:2], ("post", "https://bgm.tv/oauth/access_token"))
