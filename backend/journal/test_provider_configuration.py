from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from accounts.models import StaffProfile
from config.credentials import CredentialCipher
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from journal.external_accounts.provider_configuration import (
    clear_provider_client_secret,
    get_effective_provider_configuration,
    update_provider_configuration,
)
from journal.external_accounts.providers.bangumi import BangumiAccountProvider
from journal.models import AdminAuditLog, ExternalProviderConfiguration

User = get_user_model()
TEST_KEY = "a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io="
CALLBACK = "https://animemo.cc/api/v1/external-accounts/bangumi/callback/"


@override_settings(
    CREDENTIAL_ENCRYPTION_KEY=TEST_KEY,
    BANGUMI_ACCOUNT_INTEGRATION_ENABLED=True,
    BANGUMI_OAUTH_CLIENT_ID="environment-id",
    BANGUMI_OAUTH_CLIENT_SECRET="environment-secret",
    BANGUMI_OAUTH_REDIRECT_URI=CALLBACK,
)
class ExternalProviderConfigurationServiceTests(APITestCase):
    def test_environment_is_used_until_database_overrides_are_explicit(self):
        effective = get_effective_provider_configuration("bangumi")

        self.assertTrue(effective.enabled)
        self.assertEqual(effective.enabled_source, "environment")
        self.assertEqual(effective.client_id, "environment-id")
        self.assertEqual(effective.client_id_source, "environment")
        self.assertEqual(effective.client_secret_source, "environment")
        self.assertTrue(effective.oauth_available)
        self.assertFalse(ExternalProviderConfiguration.objects.exists())

    def test_database_values_override_environment_and_secret_is_encrypted(self):
        effective = update_provider_configuration(
            "bangumi",
            enabled=True,
            client_id="database-id",
            client_secret="database-secret",
            fields={"enabled", "client_id", "client_secret"},
        )

        stored = ExternalProviderConfiguration.objects.get(provider="bangumi")
        self.assertEqual(stored.client_id, "database-id")
        self.assertNotIn("database-secret", stored.encrypted_client_secret)
        self.assertEqual(CredentialCipher.decrypt(stored.encrypted_client_secret), "database-secret")
        self.assertEqual(stored.credential_key_version, CredentialCipher.version)
        self.assertEqual(effective.client_id_source, "database")
        self.assertEqual(effective.client_secret_source, "database")
        self.assertTrue(effective.oauth_available)

    def test_clearing_database_secret_falls_back_to_environment(self):
        update_provider_configuration(
            "bangumi",
            client_secret="database-secret",
            fields={"client_secret"},
        )

        effective = clear_provider_client_secret("bangumi")

        stored = ExternalProviderConfiguration.objects.get(provider="bangumi")
        self.assertEqual(stored.encrypted_client_secret, "")
        self.assertEqual(stored.credential_key_version, "")
        self.assertEqual(effective.client_secret, "environment-secret")
        self.assertEqual(effective.client_secret_source, "environment")
        self.assertTrue(effective.oauth_available)

    def test_empty_database_client_id_falls_back_and_disabled_override_only_blocks_oauth(self):
        effective = update_provider_configuration(
            "bangumi",
            enabled=False,
            client_id="",
            fields={"enabled", "client_id"},
        )
        provider = BangumiAccountProvider()

        self.assertEqual(effective.client_id, "environment-id")
        self.assertEqual(effective.client_id_source, "environment")
        self.assertFalse(effective.oauth_available)
        self.assertTrue(provider.enabled())
        self.assertTrue(provider.capabilities()["personal_access_token_available"])
        self.assertTrue(provider.capabilities()["media_search_available"])
        self.assertFalse(provider.capabilities()["oauth_available"])

    @override_settings(BANGUMI_OAUTH_CLIENT_SECRET="")
    def test_incomplete_effective_configuration_blocks_oauth(self):
        effective = get_effective_provider_configuration("bangumi")

        self.assertFalse(effective.client_secret_configured)
        self.assertEqual(effective.client_secret_source, "not_configured")
        self.assertFalse(effective.oauth_available)

    @override_settings(BANGUMI_OAUTH_REDIRECT_URI="https://animemo.cc/arbitrary-callback/")
    def test_noncanonical_callback_blocks_oauth(self):
        self.assertFalse(get_effective_provider_configuration("bangumi").oauth_available)

    def test_provider_oauth_request_uses_database_credentials_and_canonical_callback(self):
        update_provider_configuration(
            "bangumi",
            client_id="database-id",
            client_secret="database-secret",
            fields={"client_id", "client_secret"},
        )

        parsed = urlparse(BangumiAccountProvider().authorization_url("state-value"))
        query = parse_qs(parsed.query)
        self.assertEqual(query["client_id"], ["database-id"])
        self.assertEqual(query["redirect_uri"], [CALLBACK])

    def test_oauth_exchange_uses_decrypted_database_secret(self):
        update_provider_configuration(
            "bangumi",
            client_id="database-id",
            client_secret="database-secret",
            fields={"client_id", "client_secret"},
        )
        provider = BangumiAccountProvider()

        with patch.object(provider, "_request_json") as request_json:
            request_json.return_value = {
                "access_token": "access-token-value",
                "refresh_token": "refresh-token-value",
                "expires_in": 604800,
            }
            provider.exchange_code("authorization-code", "state-value")

        sent = request_json.call_args.kwargs["data"]
        self.assertEqual(sent["client_id"], "database-id")
        self.assertEqual(sent["client_secret"], "database-secret")
        self.assertEqual(sent["redirect_uri"], CALLBACK)

    def test_corrupt_database_secret_fails_closed_without_environment_fallback(self):
        ExternalProviderConfiguration.objects.create(
            provider="bangumi",
            enabled=True,
            client_id="database-id",
            encrypted_client_secret="v1:not-a-valid-fernet-token",
            credential_key_version=CredentialCipher.version,
        )

        effective = get_effective_provider_configuration("bangumi")

        self.assertEqual(effective.client_secret_source, "database")
        self.assertTrue(effective.client_secret_configured)
        self.assertEqual(effective.client_secret, "")
        self.assertFalse(effective.oauth_available)

    def test_wrong_database_secret_key_fails_closed_without_environment_fallback(self):
        stored_secret = CredentialCipher.encrypt("database-secret")
        ExternalProviderConfiguration.objects.create(
            provider="bangumi",
            enabled=True,
            client_id="database-id",
            encrypted_client_secret=stored_secret,
            credential_key_version=CredentialCipher.version,
        )

        with override_settings(CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii")):
            effective = get_effective_provider_configuration("bangumi")

        self.assertEqual(effective.client_secret_source, "database")
        self.assertTrue(effective.client_secret_configured)
        self.assertEqual(effective.client_secret, "")
        self.assertFalse(effective.oauth_available)


@override_settings(
    CREDENTIAL_ENCRYPTION_KEY=TEST_KEY,
    BANGUMI_ACCOUNT_INTEGRATION_ENABLED=True,
    BANGUMI_OAUTH_CLIENT_ID="environment-id",
    BANGUMI_OAUTH_CLIENT_SECRET="environment-secret",
    BANGUMI_OAUTH_REDIRECT_URI=CALLBACK,
)
class ExternalProviderConfigurationApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="provider-admin",
            password="StrongPass123!",
            is_staff=True,
        )
        StaffProfile.objects.create(user=self.admin, role=StaffProfile.Role.ADMINISTRATOR)
        self.client.force_authenticate(self.admin)
        self.detail_url = reverse(
            "staff-external-provider-configuration",
            kwargs={"provider": "bangumi"},
        )
        self.secret_url = reverse(
            "staff-external-provider-client-secret",
            kwargs={"provider": "bangumi"},
        )

    def test_get_returns_masked_metadata_only(self):
        update_provider_configuration(
            "bangumi",
            client_id="database-id",
            client_secret="never-return-this",
            fields={"client_id", "client_secret"},
        )

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["client_id"], "database-id")
        self.assertTrue(response.data["client_secret_configured"])
        self.assertEqual(response.data["client_secret_source"], "database")
        self.assertNotIn("client_secret", response.data)
        self.assertNotIn("encrypted_client_secret", response.data)
        self.assertNotIn("never-return-this", str(response.data))

    def test_corrupt_database_secret_staff_get_is_masked_and_unavailable(self):
        ExternalProviderConfiguration.objects.create(
            provider="bangumi",
            enabled=True,
            client_id="database-id",
            encrypted_client_secret="v1:not-a-valid-fernet-token",
            credential_key_version=CredentialCipher.version,
        )

        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["client_secret_configured"])
        self.assertEqual(response.data["client_secret_source"], "database")
        self.assertFalse(response.data["oauth_available"])
        self.assertNotIn("client_secret", response.data)
        self.assertNotIn("environment-secret", str(response.data))

    def test_wrong_database_secret_key_staff_get_is_masked_and_unavailable(self):
        ExternalProviderConfiguration.objects.create(
            provider="bangumi",
            enabled=True,
            client_id="database-id",
            encrypted_client_secret=CredentialCipher.encrypt("database-secret"),
            credential_key_version=CredentialCipher.version,
        )

        with override_settings(CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii")):
            response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["client_secret_configured"])
        self.assertEqual(response.data["client_secret_source"], "database")
        self.assertFalse(response.data["oauth_available"])
        self.assertNotIn("client_secret", response.data)
        self.assertNotIn("database-secret", str(response.data))
        self.assertNotIn("environment-secret", str(response.data))

    def test_patch_replaces_secret_without_leaking_it_to_response_or_audit(self):
        response = self.client.patch(
            self.detail_url,
            {"enabled": True, "client_id": "database-id", "client_secret": "new-secret-value"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["oauth_available"])
        self.assertNotIn("new-secret-value", str(response.data))
        audit = AdminAuditLog.objects.get(action="provider_configuration.update")
        self.assertNotIn("new-secret-value", str(audit.before))
        self.assertNotIn("new-secret-value", str(audit.after))
        self.assertNotIn("new-secret-value", str(audit.metadata))

    def test_delete_database_secret_returns_masked_environment_fallback(self):
        update_provider_configuration(
            "bangumi",
            client_secret="database-secret",
            fields={"client_secret"},
        )

        response = self.client.delete(self.secret_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["client_secret_configured"])
        self.assertEqual(response.data["client_secret_source"], "environment")
        self.assertNotIn("environment-secret", str(response.data))

    def test_callback_is_read_only(self):
        response = self.client.patch(
            self.detail_url,
            {"oauth_callback": "https://attacker.test/callback/"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(get_effective_provider_configuration("bangumi").oauth_callback, CALLBACK)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account")
    def test_disabled_oauth_integration_does_not_block_pat_connection(self, verify_account):
        update_provider_configuration("bangumi", enabled=False, fields={"enabled"})
        verify_account.return_value = {
            "external_user_id": "100",
            "external_username": "pat-user",
            "display_name": "PAT User",
            "metadata": {"avatar_url": ""},
        }

        response = self.client.post(
            reverse("external-account-connect", kwargs={"provider": "bangumi"}),
            {"access_token": "personal-access-token"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["auth_method"], "personal_access_token")

    def test_plain_staff_flag_and_regular_user_cannot_access_provider_configuration(self):
        plain_staff = User.objects.create_user(
            username="plain-staff",
            password="StrongPass123!",
            is_staff=True,
        )
        self.client.force_authenticate(plain_staff)
        self.assertEqual(self.client.get(self.detail_url).status_code, status.HTTP_403_FORBIDDEN)

        regular = User.objects.create_user(username="regular-user", password="StrongPass123!")
        self.client.force_authenticate(regular)
        self.assertEqual(self.client.get(self.detail_url).status_code, status.HTTP_403_FORBIDDEN)
        denied = self.client.patch(
            self.detail_url,
            {"client_secret": "must-not-be-stored"},
            format="json",
        )
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(ExternalProviderConfiguration.objects.exists())

    @override_settings(CREDENTIAL_ENCRYPTION_KEY="")
    def test_encryption_failure_is_generic_and_rolls_back(self):
        response = self.client.patch(
            self.detail_url,
            {"client_secret": "must-not-leak"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE, response.data)
        self.assertNotIn("must-not-leak", str(response.data))
        self.assertFalse(ExternalProviderConfiguration.objects.exists())
        self.assertFalse(AdminAuditLog.objects.exists())

    def test_unsupported_provider_is_not_exposed(self):
        response = self.client.get(
            reverse("staff-external-provider-configuration", kwargs={"provider": "unknown"})
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
