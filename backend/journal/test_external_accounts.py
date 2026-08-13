from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from config.credentials import CredentialCipher
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from journal.external_accounts.credentials import (
    decrypt_credentials,
    encrypt_credentials,
)
from journal.external_accounts.errors import (
    ExternalAccountError,
    account_token_invalid,
    provider_unavailable,
)
from journal.external_accounts.services import (
    apply_import_preview,
    complete_oauth_authorization,
)
from journal.external_media.services import PreparedIdentity
from journal.models import (
    ExternalAccountAuthorizationState,
    ExternalImportSession,
    ExternalMediaIdentity,
    JournalEntry,
    UserExternalAccountConnection,
)

User = get_user_model()
TEST_KEY = "a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io="


def profile(external_user_id="100", username="bangumi-user"):
    return {
        "external_user_id": str(external_user_id),
        "external_username": username,
        "display_name": "Bangumi User",
        "metadata": {"avatar_url": "https://lain.bgm.tv/pic/user/l/100.jpg"},
    }


def remote_row(external_id="1424", **overrides):
    row = {
        "provider": "bangumi",
        "external_id": str(external_id),
        "title": "轻音少女",
        "japanese_title": "けいおん！",
        "poster_url": "",
        "remote_status": "completed",
        "remote_status_label": "看过",
        "remote_status_code": 2,
        "remote_rating": 9,
        "remote_comment": "远端短评",
        "remote_comment_summary": "远端短评",
        "remote_tags": ["收藏标签"],
        "remote_updated_at": "2026-08-09T00:00:00+08:00",
    }
    row.update(overrides)
    return row


def prepared(external_id="1424"):
    return PreparedIdentity(
        provider="bangumi",
        external_id=str(external_id),
        canonical_url=f"https://bgm.tv/subject/{external_id}",
        metadata={
            "title": "轻音少女",
            "japanese_title": "けいおん！",
            "summary": "作品简介",
            "episodes": 14,
            "air_date": "2009-04-03",
            "studio": "京都アニメーション",
            "tags": ["校园", "日常"],
            "score": 8.2,
            "poster_url": "",
            "thumbnail_url": "",
            "provider_name": "Bangumi",
            "provider_url": f"https://bgm.tv/subject/{external_id}",
            "external_id": str(external_id),
        },
        metadata_fetched_at=timezone.now(),
    )


@override_settings(
    CREDENTIAL_ENCRYPTION_KEY=TEST_KEY,
    BANGUMI_ACCOUNT_INTEGRATION_ENABLED=True,
    EXTERNAL_ACCOUNT_OAUTH_STATE_TTL_SECONDS=600,
    BANGUMI_IMPORT_MAX_ITEMS=1000,
    EXTERNAL_IMPORT_PREVIEW_TTL_SECONDS=1200,
    EXTERNAL_IMPORT_APPLY_MAX_ITEMS=100,
)
class ExternalAccountApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="account-owner", password="StrongPass123!")
        self.other = User.objects.create_user(username="account-other", password="StrongPass123!")
        self.client.force_authenticate(self.user)

    def create_connection(self, user=None, external_user_id="100"):
        user = user or self.user
        return UserExternalAccountConnection.objects.create(
            user=user,
            provider="bangumi",
            auth_method="personal_access_token",
            external_user_id=str(external_user_id),
            external_username=f"user-{external_user_id}",
            display_name="Bangumi User",
            credential_ciphertext=encrypt_credentials({"access_token": "test-token-value"}),
            credential_key_version=CredentialCipher.version,
            metadata={"avatar_url": ""},
            connected_at=timezone.now(),
            verified_at=timezone.now(),
        )

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    def test_connect_verifies_and_encrypts_token_without_api_leak(self, verify):
        verify.return_value = profile()
        response = self.client.post(
            reverse("external-account-connect", kwargs={"provider": "bangumi"}),
            {"access_token": "plain-token-value"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        connection = UserExternalAccountConnection.objects.get(user=self.user)
        self.assertNotIn("plain-token-value", connection.credential_ciphertext)
        self.assertEqual(decrypt_credentials(connection.credential_ciphertext)["access_token"], "plain-token-value")
        self.assertNotIn("credential", str(response.data).lower())
        self.assertNotIn("plain-token-value", str(response.data))
        self.assertNotIn("access_token", response.data)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    def test_invalid_token_is_rejected_without_row(self, verify):
        verify.side_effect = account_token_invalid()
        response = self.client.post(
            reverse("external-account-connect", kwargs={"provider": "bangumi"}),
            {"access_token": "invalid-token"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "external_account_token_invalid")
        self.assertFalse(UserExternalAccountConnection.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    def test_reauthorize_same_identity_rotates_but_different_identity_is_denied(self, verify):
        self.create_connection()
        verify.return_value = profile()
        ok = self.client.post(reverse("external-account-connect", kwargs={"provider": "bangumi"}), {"access_token": "rotated-token"}, format="json")
        self.assertEqual(ok.status_code, status.HTTP_201_CREATED)
        self.assertEqual(decrypt_credentials(UserExternalAccountConnection.objects.get().credential_ciphertext)["access_token"], "rotated-token")
        verify.return_value = profile("200", "other-bangumi-user")
        denied = self.client.post(reverse("external-account-connect", kwargs={"provider": "bangumi"}), {"access_token": "other-token"}, format="json")
        self.assertEqual(denied.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(denied.data["code"], "external_account_identity_mismatch")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    def test_same_external_identity_cannot_be_connected_by_another_user(self, verify):
        self.create_connection(external_user_id="100")
        self.client.force_authenticate(self.other)
        verify.return_value = profile("100")
        response = self.client.post(
            reverse("external-account-connect", kwargs={"provider": "bangumi"}),
            {"access_token": "other-user-token"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "external_account_already_connected")
        self.assertEqual(UserExternalAccountConnection.objects.count(), 1)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    def test_provider_timeout_does_not_create_connection(self, verify):
        verify.side_effect = provider_unavailable()
        response = self.client.post(
            reverse("external-account-connect", kwargs={"provider": "bangumi"}),
            {"access_token": "timeout-token"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["code"], "provider_unavailable")
        self.assertFalse(UserExternalAccountConnection.objects.exists())

    def test_list_never_serializes_ciphertext_and_cross_user_isolation(self):
        self.create_connection(self.other, "200")
        response = self.client.get(reverse("external-account-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["providers"][0]["connection"])
        self.assertNotIn("cipher", str(response.data).lower())

    def test_provider_list_comes_from_registry_capabilities(self):
        response = self.client.get(reverse("external-account-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        capability = response.data["providers"][0]
        self.assertEqual(capability["provider"], "bangumi")
        self.assertTrue(capability["media_search_available"])
        self.assertTrue(capability["account_connection_available"])
        self.assertTrue(capability["import_available"])

    def test_disconnect_preserves_entries_and_external_identity(self):
        self.create_connection()
        entry = JournalEntry.objects.create(user=self.user, title="已导入")
        identity = ExternalMediaIdentity.objects.create(
            entry=entry,
            provider="bangumi",
            external_id="1424",
            canonical_url="https://bgm.tv/subject/1424",
        )
        response = self.client.delete(reverse("external-account-detail", kwargs={"provider": "bangumi"}))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(JournalEntry.objects.filter(pk=entry.pk).exists())
        self.assertTrue(ExternalMediaIdentity.objects.filter(pk=identity.pk).exists())

    @override_settings(BANGUMI_ACCOUNT_INTEGRATION_ENABLED=False)
    def test_disabled_integration_still_allows_local_credential_deletion(self):
        self.create_connection()
        response = self.client.delete(reverse("external-account-detail", kwargs={"provider": "bangumi"}))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(UserExternalAccountConnection.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    def test_verify_marks_revoked_connection_for_reauthorization(self, verify):
        connection = self.create_connection()
        verify.side_effect = account_token_invalid()
        response = self.client.post(reverse("external-account-verify", kwargs={"provider": "bangumi"}), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        connection.refresh_from_db()
        self.assertEqual(connection.status, "needs_reauthorization")

    @override_settings(
        BANGUMI_OAUTH_CLIENT_ID="client-id",
        BANGUMI_OAUTH_CLIENT_SECRET="client-secret",
        BANGUMI_OAUTH_REDIRECT_URI="https://example.test/api/v1/external-accounts/bangumi/callback/",
    )
    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.refresh_oauth_token")
    def test_expired_oauth_token_is_refreshed_and_reencrypted(self, refresh, verify):
        connection = self.create_connection()
        connection.auth_method = "oauth"
        connection.credential_ciphertext = encrypt_credentials({
            "access_token": "expired-access-token",
            "refresh_token": "refresh-token-value",
            "expires_in": 1,
        })
        connection.expires_at = timezone.now() - timedelta(seconds=1)
        connection.save()
        refresh.return_value = {
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
            "expires_in": 604800,
            "token_type": "Bearer",
        }
        verify.return_value = profile()
        response = self.client.post(
            reverse("external-account-verify", kwargs={"provider": "bangumi"}),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        connection.refresh_from_db()
        stored = decrypt_credentials(connection.credential_ciphertext)
        self.assertEqual(stored["access_token"], "fresh-access-token")
        self.assertNotIn("fresh-access-token", connection.credential_ciphertext)
        self.assertEqual(connection.status, "connected")

    @override_settings(
        BANGUMI_OAUTH_CLIENT_ID="client-id",
        BANGUMI_OAUTH_CLIENT_SECRET="client-secret",
        BANGUMI_OAUTH_REDIRECT_URI="https://example.test/api/v1/external-accounts/bangumi/callback/",
    )
    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.refresh_oauth_token")
    def test_expired_oauth_refresh_failure_persists_reauthorization(self, refresh):
        connection = self.create_connection()
        connection.auth_method = "oauth"
        connection.credential_ciphertext = encrypt_credentials({
            "access_token": "expired-access-token",
            "refresh_token": "refresh-token-value",
            "expires_in": 1,
        })
        connection.expires_at = timezone.now() - timedelta(seconds=1)
        connection.save()
        refresh.side_effect = account_token_invalid()

        response = self.client.post(
            reverse("external-account-verify", kwargs={"provider": "bangumi"}),
            {},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        connection.refresh_from_db()
        self.assertEqual(connection.status, "needs_reauthorization")

    @override_settings(
        BANGUMI_OAUTH_CLIENT_ID="client-id",
        BANGUMI_OAUTH_CLIENT_SECRET="client-secret",
        BANGUMI_OAUTH_REDIRECT_URI="https://example.test/api/v1/external-accounts/bangumi/callback/",
    )
    def test_oauth_state_is_hashed_bound_single_use_and_callback_hides_code(self):
        authorize = self.client.post(reverse("external-account-authorize", kwargs={"provider": "bangumi"}), {}, format="json")
        self.assertEqual(authorize.status_code, status.HTTP_200_OK, authorize.data)
        state = parse_qs(urlparse(authorize.data["authorization_url"]).query)["state"][0]
        stored = ExternalAccountAuthorizationState.objects.get()
        self.assertEqual(stored.user, self.user)
        self.assertNotEqual(stored.state_digest, state)
        with patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.exchange_code") as exchange, patch(
            "journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account"
        ) as verify:
            exchange.return_value = {"access_token": "oauth-access-token", "refresh_token": "oauth-refresh-token", "expires_in": 604800, "token_type": "Bearer"}
            verify.return_value = profile()
            callback = self.client.get(reverse("external-account-callback", kwargs={"provider": "bangumi"}), {"code": "secret-code", "state": state})
        self.assertEqual(callback.status_code, status.HTTP_302_FOUND)
        self.assertNotIn("secret-code", callback["Location"])
        self.assertNotIn("oauth-access-token", callback["Location"])
        stored.refresh_from_db()
        self.assertIsNotNone(stored.consumed_at)
        with self.assertRaises(ExternalAccountError) as replay:
            complete_oauth_authorization(provider_slug="bangumi", code="replay-code", state=state)
        self.assertEqual(replay.exception.detail["code"], "authorization_state_invalid")

    @override_settings(
        BANGUMI_OAUTH_CLIENT_ID="client-id",
        BANGUMI_OAUTH_CLIENT_SECRET="client-secret",
        BANGUMI_OAUTH_REDIRECT_URI="https://example.test/api/v1/external-accounts/bangumi/callback/",
    )
    def test_oauth_states_are_random_and_expired_state_is_denied(self):
        first = self.client.post(reverse("external-account-authorize", kwargs={"provider": "bangumi"}), {}, format="json")
        second = self.client.post(reverse("external-account-authorize", kwargs={"provider": "bangumi"}), {}, format="json")
        first_state = parse_qs(urlparse(first.data["authorization_url"]).query)["state"][0]
        second_state = parse_qs(urlparse(second.data["authorization_url"]).query)["state"][0]
        self.assertNotEqual(first_state, second_state)
        stored = ExternalAccountAuthorizationState.objects.order_by("created_at").first()
        stored.expires_at = timezone.now() - timedelta(seconds=1)
        stored.save(update_fields=["expires_at"])
        callback = self.client.get(
            reverse("external-account-callback", kwargs={"provider": "bangumi"}),
            {"code": "unused-code", "state": first_state},
        )
        self.assertEqual(callback.status_code, status.HTTP_302_FOUND)
        self.assertIn("external_account_status=error", callback["Location"])
        self.assertIn("external_account_provider=bangumi", callback["Location"])
        self.assertIn("authorization_state_expired", callback["Location"])

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collections")
    def test_preview_matches_identity_authoritatively_and_title_only_as_possible(self, collections):
        self.create_connection()
        bound = JournalEntry.objects.create(user=self.user, title="已绑定")
        ExternalMediaIdentity.objects.create(entry=bound, provider="bangumi", external_id="1424", canonical_url="https://bgm.tv/subject/1424")
        JournalEntry.objects.create(user=self.user, title="同名作品")
        collections.return_value = [remote_row(), remote_row("200", title="同名作品", japanese_title="")]
        response = self.client.post(reverse("external-account-import-preview", kwargs={"provider": "bangumi"}), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        by_id = {row["external_id"]: row for row in response.data["results"]}
        self.assertEqual(by_id["1424"]["match_state"], "already_bound")
        self.assertEqual(by_id["1424"]["local_entry_id"], bound.pk)
        self.assertEqual(by_id["200"]["match_state"], "possible_local_match")
        self.assertIsNone(by_id["200"]["local_entry_id"])

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collections")
    def test_preview_provider_failure_and_paging_are_bounded(self, collections):
        self.create_connection()
        collections.side_effect = provider_unavailable()
        failed = self.client.post(reverse("external-account-import-preview", kwargs={"provider": "bangumi"}), {}, format="json")
        self.assertEqual(failed.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        collections.side_effect = None
        collections.return_value = [remote_row("1"), remote_row("2", remote_status="watching"), remote_row("3")]
        created = self.client.post(reverse("external-account-import-preview", kwargs={"provider": "bangumi"}), {}, format="json")
        preview_id = created.data["preview_id"]
        page = self.client.get(
            reverse("external-account-import-preview-detail", kwargs={"provider": "bangumi", "preview_id": preview_id}),
            {"page": 2, "page_size": 1, "filter": "all"},
        )
        self.assertEqual(page.status_code, status.HTTP_200_OK, page.data)
        self.assertEqual(page.data["total"], 3)
        self.assertEqual(page.data["results"][0]["external_id"], "2")

    @patch("journal.external_accounts.imports.prepare_identity")
    def test_apply_create_is_atomic_uses_snapshot_and_is_idempotent(self, prepare):
        session = ExternalImportSession.objects.create(
            user=self.user,
            provider="bangumi",
            snapshot=[remote_row()],
            expires_at=timezone.now() + timedelta(minutes=20),
        )
        prepare.return_value = prepared()
        payload = [{"external_id": "1424", "mode": "CREATE_NEW"}]
        first = apply_import_preview(user=self.user, provider_slug="bangumi", preview_id=session.pk, items=payload)
        second = apply_import_preview(user=self.user, provider_slug="bangumi", preview_id=session.pk, items=payload)
        self.assertEqual(first, second)
        self.assertEqual(first["counts"]["created"], 1)
        self.assertEqual(JournalEntry.objects.filter(user=self.user).count(), 1)
        entry = JournalEntry.objects.get(user=self.user)
        self.assertEqual(str(entry.personal_score), "9.00")
        self.assertEqual(entry.watch_status, "completed")
        self.assertEqual(entry.review, "远端短评")
        self.assertTrue(ExternalMediaIdentity.objects.filter(entry=entry, external_id="1424").exists())

    @patch("journal.external_accounts.imports.prepare_identity")
    def test_bind_existing_preserves_local_fields_by_default_and_explicitly_overrides_selected(self, prepare):
        entry = JournalEntry.objects.create(
            user=self.user,
            title="本地作品",
            personal_score="8.00",
            watch_status="watching",
            review="本地评价",
        )
        first = ExternalImportSession.objects.create(
            user=self.user,
            provider="bangumi",
            snapshot=[remote_row()],
            expires_at=timezone.now() + timedelta(minutes=20),
        )
        prepare.return_value = prepared()
        result = apply_import_preview(
            user=self.user,
            provider_slug="bangumi",
            preview_id=first.pk,
            items=[{"external_id": "1424", "mode": "BIND_EXISTING", "local_entry_id": entry.pk}],
        )
        self.assertEqual(result["counts"]["bound"], 1)
        entry.refresh_from_db()
        self.assertEqual(str(entry.personal_score), "8.00")
        self.assertEqual(entry.watch_status, "watching")
        self.assertEqual(entry.review, "本地评价")
        second = ExternalImportSession.objects.create(
            user=self.user,
            provider="bangumi",
            snapshot=[remote_row()],
            expires_at=timezone.now() + timedelta(minutes=20),
        )
        updated = apply_import_preview(
            user=self.user,
            provider_slug="bangumi",
            preview_id=second.pk,
            items=[{"external_id": "1424", "mode": "IMPORT_SAFE_USER_FIELDS", "apply_fields": ["personal_score", "watch_status"]}],
        )
        self.assertEqual(updated["counts"]["updated"], 1)
        entry.refresh_from_db()
        self.assertEqual(str(entry.personal_score), "9.00")
        self.assertEqual(entry.watch_status, "completed")
        self.assertEqual(entry.review, "本地评价")

    def test_cross_user_preview_access_is_denied_without_disclosure(self):
        session = ExternalImportSession.objects.create(
            user=self.other,
            provider="bangumi",
            snapshot=[remote_row()],
            expires_at=timezone.now() + timedelta(minutes=20),
        )
        response = self.client.get(reverse("external-account-import-preview-detail", kwargs={"provider": "bangumi", "preview_id": session.pk}))
        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        self.assertEqual(response.data["code"], "import_preview_expired")

    def test_expired_preview_and_arbitrary_payload_are_rejected(self):
        expired = ExternalImportSession.objects.create(
            user=self.user,
            provider="bangumi",
            snapshot=[remote_row()],
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = self.client.post(
            reverse("external-account-import-apply", kwargs={"provider": "bangumi"}),
            {"preview_id": str(expired.pk), "items": [{"external_id": "1424", "mode": "CREATE_NEW", "title": "不可信标题"}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_410_GONE)
        self.assertFalse(JournalEntry.objects.exists())

    def test_cleanup_command_removes_only_expired_external_sessions(self):
        expired_state = ExternalAccountAuthorizationState.objects.create(
            user=self.user,
            provider="bangumi",
            state_digest="a" * 64,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        active_state = ExternalAccountAuthorizationState.objects.create(
            user=self.user,
            provider="bangumi",
            state_digest="b" * 64,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        expired_import = ExternalImportSession.objects.create(
            user=self.user,
            provider="bangumi",
            snapshot=[],
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        active_import = ExternalImportSession.objects.create(
            user=self.user,
            provider="bangumi",
            snapshot=[],
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        call_command("cleanup_external_account_sessions", verbosity=0)

        self.assertFalse(ExternalAccountAuthorizationState.objects.filter(pk=expired_state.pk).exists())
        self.assertTrue(ExternalAccountAuthorizationState.objects.filter(pk=active_state.pk).exists())
        self.assertFalse(ExternalImportSession.objects.filter(pk=expired_import.pk).exists())
        self.assertTrue(ExternalImportSession.objects.filter(pk=active_import.pk).exists())

    @patch("journal.external_accounts.imports.prepare_identity")
    def test_malformed_item_is_isolated_and_client_cannot_tamper_with_snapshot(self, prepare):
        session = ExternalImportSession.objects.create(
            user=self.user,
            provider="bangumi",
            snapshot=[remote_row()],
            expires_at=timezone.now() + timedelta(minutes=20),
        )
        prepare.return_value = prepared()
        result = apply_import_preview(
            user=self.user,
            provider_slug="bangumi",
            preview_id=session.pk,
            items=[
                {"external_id": "malformed", "mode": "WRITE_REMOTE"},
                {"external_id": "200", "mode": "BIND_EXISTING", "local_entry_id": "not-an-id"},
                {
                    "external_id": "1424",
                    "mode": "CREATE_NEW",
                    "title": "客户端伪造标题",
                    "remote_rating": 1,
                    "remote_comment": "客户端伪造评论",
                },
            ],
        )
        self.assertEqual(result["counts"]["failed"], 2)
        self.assertEqual(result["counts"]["created"], 1)
        entry = JournalEntry.objects.get(user=self.user)
        self.assertEqual(entry.title, "轻音少女")
        self.assertEqual(str(entry.personal_score), "9.00")
        self.assertEqual(entry.review, "远端短评")
