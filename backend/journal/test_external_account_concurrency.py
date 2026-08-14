from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, Lock
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from journal.external_accounts.connections import connection_token_for_use
from journal.external_accounts.credentials import decrypt_credentials, encrypt_credentials
from journal.models import UserExternalAccountConnection


@skipUnless(connection.vendor == "postgresql", "OAuth refresh concurrency proof requires PostgreSQL")
@override_settings(
    BANGUMI_OAUTH_CLIENT_ID="client-id",
    BANGUMI_OAUTH_CLIENT_SECRET="client-secret",
    BANGUMI_OAUTH_REDIRECT_URI="https://example.test/api/v1/external-accounts/bangumi/callback/",
)
class ExternalAccountRefreshPostgreSQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="oauth-refresh-race",
            password="StrongPass123!",
        )
        self.connection = UserExternalAccountConnection.objects.create(
            user=self.user,
            provider="bangumi",
            auth_method=UserExternalAccountConnection.AuthMethod.OAUTH,
            external_user_id="100",
            external_username="oauth-refresh-race",
            credential_ciphertext=encrypt_credentials({
                "access_token": "expired-access-token",
                "refresh_token": "single-use-refresh-token",
                "expires_in": 1,
            }),
            connected_at=timezone.now(),
            expires_at=timezone.now() - timedelta(seconds=1),
        )

    def test_concurrent_requests_refresh_once_and_share_rotated_credentials(self):
        workers_ready = Barrier(2)
        refresh_started = Event()
        second_refresh_started = Event()
        release_refresh = Event()
        refresh_count = 0
        refresh_count_lock = Lock()

        def refresh(_provider, refresh_token):
            nonlocal refresh_count
            self.assertEqual(refresh_token, "single-use-refresh-token")
            with refresh_count_lock:
                refresh_count += 1
                if refresh_count == 2:
                    second_refresh_started.set()
            refresh_started.set()
            self.assertTrue(release_refresh.wait(timeout=10))
            return {
                "access_token": "fresh-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 604800,
                "token_type": "Bearer",
            }

        def verify(_provider, access_token):
            self.assertEqual(access_token, "fresh-access-token")
            return {
                "external_user_id": "100",
                "external_username": "oauth-refresh-race",
                "display_name": "OAuth Refresh Race",
                "metadata": {},
            }

        def fetch_token():
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                workers_ready.wait(timeout=10)
                return connection_token_for_use(user=user, provider_slug="bangumi")[2]
            finally:
                connections.close_all()

        with patch(
            "journal.external_accounts.providers.bangumi.BangumiAccountProvider.refresh_oauth_token",
            autospec=True,
            side_effect=refresh,
        ), patch(
            "journal.external_accounts.providers.bangumi.BangumiAccountProvider.verify_account",
            autospec=True,
            side_effect=verify,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fetch_token) for _ in range(2)]
            self.assertTrue(refresh_started.wait(timeout=10))
            second_refresh_started.wait(timeout=0.75)
            release_refresh.set()
            tokens = [future.result(timeout=15) for future in futures]

        self.assertEqual(tokens, ["fresh-access-token", "fresh-access-token"])
        self.assertEqual(refresh_count, 1)
        self.connection.refresh_from_db()
        stored = decrypt_credentials(self.connection.credential_ciphertext)
        self.assertEqual(stored["access_token"], "fresh-access-token")
        self.assertEqual(stored["refresh_token"], "rotated-refresh-token")
        self.assertEqual(self.connection.status, UserExternalAccountConnection.Status.CONNECTED)
