from copy import deepcopy
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from journal.external_accounts.credentials import encrypt_credentials
from journal.external_sync.canonical import canonical_snapshot
from journal.external_sync.confirmation import CONFIRMATION_KEYS, decode_preview_token
from journal.models import (
    ExternalCollectionSyncState,
    ExternalMediaIdentity,
    JournalEntry,
    UserExternalAccountConnection,
    WatchHistoryRecord,
)
from journal.watch_history import add_history

User = get_user_model()

RELAXED_SYNC_THROTTLES = {
    **settings.REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
        "external_sync_preview": "1000/min",
        "external_sync_apply": "1000/min",
    },
}


def remote_collection(
    *,
    external_id="1424",
    watch_status="watching",
    score=8,
    score_present=True,
    review="远端短评",
    review_present=True,
):
    return {
        "provider": "bangumi",
        "external_id": external_id,
        "remote_status": watch_status,
        "remote_rating": score if score_present else None,
        "remote_rating_present": score_present,
        "remote_comment": review if review_present else "",
        "remote_comment_present": review_present,
    }


def field_plan(payload, field):
    return next(item for item in payload["fields"] if item["field"] == field)


@override_settings(REST_FRAMEWORK=RELAXED_SYNC_THROTTLES)
class ManualCollectionPullTests(APITransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(username="apply-owner", password="StrongPass123!")
        self.other = User.objects.create_user(username="apply-other", password="StrongPass123!")
        self.entry = JournalEntry.objects.create(
            user=self.user,
            title="显式拉取作品",
            watch_status="watching",
            personal_score="8.50",
            review="本地短评",
        )
        self.identity = ExternalMediaIdentity.objects.create(
            entry=self.entry,
            provider="bangumi",
            external_id="1424",
            canonical_url="https://bgm.tv/subject/1424",
        )
        self.account_connection = UserExternalAccountConnection.objects.create(
            user=self.user,
            provider="bangumi",
            auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
            external_user_id="100",
            external_username="apply-owner",
            credential_ciphertext=encrypt_credentials(
                {"access_token": "test-access-token", "token_type": "Bearer"}
            ),
            connected_at=timezone.now(),
        )
        self.preview_url = reverse(
            "external-collection-sync-preview",
            kwargs={"provider": "bangumi", "entry_id": self.entry.pk},
        )
        self.apply_url = reverse(
            "external-collection-sync-apply",
            kwargs={"provider": "bangumi", "entry_id": self.entry.pk},
        )
        self.client.force_authenticate(self.user)

    def preview(self, get_collection, remote=None):
        get_collection.return_value = remote if remote is not None else remote_collection()
        response = self.client.get(self.preview_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def apply(self, token, actions):
        return self.client.post(
            self.apply_url,
            {"preview_token": token, "actions": actions},
            format="json",
        )

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_accept_equal_initializes_only_selected_baseline(self, get_collection):
        remote = remote_collection(watch_status="watching", score=7, review="不同")
        preview = self.preview(get_collection, remote)
        self.assertEqual(field_plan(preview.data, "watch_status")["state"], "uninitialized_equal")

        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "accept_equal"}],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["local_updated_fields"], [])
        self.assertEqual(response.data["baseline_advanced_fields"], ["watch_status"])
        self.assertTrue(response.data["sync_state_initialized"])
        self.assertTrue(response.data["sync_state_created"])
        state = ExternalCollectionSyncState.objects.get(identity=self.identity)
        self.assertEqual(state.baselines, {"watch_status": {"present": True, "value": "watching"}})
        self.assertIsNotNone(state.last_synced_at)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.watch_status, "watching")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_converged_accept_equal_advances_without_local_mutation(self, get_collection):
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.account_connection,
            baselines={"watch_status": {"present": True, "value": "planned"}},
        )
        preview = self.preview(get_collection, remote_collection(watch_status="watching"))
        self.assertEqual(field_plan(preview.data, "watch_status")["state"], "converged")

        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "accept_equal"}],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        state.refresh_from_db()
        self.assertEqual(state.baselines["watch_status"]["value"], "watching")
        self.assertEqual(response.data["local_updated_fields"], [])
        self.assertTrue(response.data["sync_state_initialized"])
        self.assertFalse(response.data["sync_state_created"])

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_pull_remote_handles_remote_local_and_conflict_states(self, get_collection):
        cases = (
            ("remote_changed", "watching", "watching", "completed", "completed"),
            ("local_changed", "watching", "completed", "watching", "watching"),
            ("conflict", "watching", "completed", "dropped", "dropped"),
        )
        for expected_state, baseline, local, remote_status, expected in cases:
            with self.subTest(expected_state=expected_state):
                ExternalCollectionSyncState.objects.all().delete()
                JournalEntry.objects.filter(pk=self.entry.pk).update(watch_status=local)
                ExternalCollectionSyncState.objects.create(
                    identity=self.identity,
                    connection=self.account_connection,
                    baselines={"watch_status": {"present": True, "value": baseline}},
                )
                preview = self.preview(get_collection, remote_collection(watch_status=remote_status))
                self.assertEqual(field_plan(preview.data, "watch_status")["state"], expected_state)
                response = self.apply(
                    preview.data["preview_token"],
                    [{"field": "watch_status", "action": "pull_remote"}],
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
                self.entry.refresh_from_db()
                state = ExternalCollectionSyncState.objects.get(identity=self.identity)
                self.assertEqual(self.entry.watch_status, expected)
                self.assertEqual(state.baselines["watch_status"]["value"], expected)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_score_missing_clears_local_without_rounding(self, get_collection):
        remote = remote_collection(score_present=False)
        preview = self.preview(get_collection, remote)
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "personal_score", "action": "pull_remote"}],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.entry.refresh_from_db()
        state = ExternalCollectionSyncState.objects.get(identity=self.identity)
        self.assertIsNone(self.entry.personal_score)
        self.assertEqual(state.baselines["personal_score"], {"present": False, "value": None})

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_review_empty_is_pullable_but_missing_is_not_representable(self, get_collection):
        empty = remote_collection(review="", review_present=True)
        preview = self.preview(get_collection, empty)
        self.assertTrue(field_plan(preview.data, "review")["pull_supported"])
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "review", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.entry.refresh_from_db()
        state = ExternalCollectionSyncState.objects.get(identity=self.identity)
        self.assertEqual(self.entry.review, "")
        self.assertEqual(state.baselines["review"], {"present": True, "value": ""})

        ExternalCollectionSyncState.objects.all().delete()
        JournalEntry.objects.filter(pk=self.entry.pk).update(review="本地短评")
        missing = remote_collection(review_present=False)
        preview = self.preview(get_collection, missing)
        review = field_plan(preview.data, "review")
        self.assertEqual(review["state"], "unsupported")
        self.assertFalse(review["pull_supported"])
        self.assertEqual(review["pull_block_reason"], "remote_value_not_representable")
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "review", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(response.data["code"], "sync_action_not_allowed")
        self.assertFalse(ExternalCollectionSyncState.objects.exists())
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.review, "本地短评")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_partial_apply_preserves_unselected_and_existing_baselines(self, get_collection):
        self.entry.review = "共同短评"
        self.entry.save(update_fields=["review", "updated_at"])
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.account_connection,
            baselines=canonical_snapshot(watch_status="planned", personal_score=7, review="旧短评"),
        )
        remote = remote_collection(watch_status="completed", score=8, review="共同短评")
        preview = self.preview(get_collection, remote)
        response = self.apply(
            preview.data["preview_token"],
            [
                {"field": "watch_status", "action": "pull_remote"},
                {"field": "review", "action": "accept_equal"},
            ],
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["applied_fields"], ["watch_status", "review"])
        self.assertEqual(response.data["local_updated_fields"], ["watch_status"])
        self.entry.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(self.entry.watch_status, "completed")
        self.assertEqual(str(self.entry.personal_score), "8.50")
        self.assertEqual(state.baselines["watch_status"]["value"], "completed")
        self.assertEqual(state.baselines["review"]["value"], "共同短评")
        self.assertEqual(state.baselines["personal_score"]["value"], "7")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_skip_and_empty_actions_create_nothing_and_advance_nothing(self, get_collection):
        preview = self.preview(get_collection)
        for actions in (
            [],
            [{"field": "watch_status", "action": "skip"}],
        ):
            with self.subTest(actions=actions):
                response = self.apply(preview.data["preview_token"], actions)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
                self.assertEqual(response.data["code"], "no_sync_action")
                self.assertFalse(ExternalCollectionSyncState.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_keep_local_preserves_existing_baseline_and_last_synced_at(self, get_collection):
        last_synced_at = timezone.now()
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.account_connection,
            baselines={"watch_status": {"present": True, "value": "planned"}},
            last_synced_at=last_synced_at,
        )
        preview = self.preview(get_collection, remote_collection(watch_status="dropped"))
        self.assertEqual(field_plan(preview.data, "watch_status")["state"], "conflict")
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "skip"}],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(response.data["code"], "no_sync_action")
        state.refresh_from_db()
        self.entry.refresh_from_db()
        self.assertEqual(state.baselines, {"watch_status": {"present": True, "value": "planned"}})
        self.assertEqual(state.last_synced_at, last_synced_at)
        self.assertEqual(self.entry.watch_status, "watching")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_invalid_mixed_request_is_atomic_and_rejects_client_values(self, get_collection):
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))
        invalid_requests = (
            {
                "preview_token": preview.data["preview_token"],
                "actions": [
                    {"field": "watch_status", "action": "pull_remote"},
                    {"field": "review", "action": "push_local"},
                ],
            },
            {
                "preview_token": preview.data["preview_token"],
                "actions": [
                    {"field": "watch_status", "action": "pull_remote"},
                    {"field": "watch_status", "action": "skip"},
                ],
            },
            {
                "preview_token": preview.data["preview_token"],
                "actions": [{"field": "title", "action": "pull_remote"}],
            },
            {
                "preview_token": preview.data["preview_token"],
                "actions": [{"field": "watch_status", "action": "pull_remote", "remote_value": "dropped"}],
            },
            {
                "preview_token": preview.data["preview_token"],
                "actions": [{"field": "watch_status", "action": "pull_remote"}],
                "baseline": {"watch_status": "forged"},
            },
        )
        for payload in invalid_requests:
            with self.subTest(payload=payload):
                response = self.client.post(self.apply_url, payload, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
                self.assertEqual(response.data["code"], "sync_request_invalid")
                self.entry.refresh_from_db()
                self.assertEqual(self.entry.watch_status, "watching")
                self.assertFalse(ExternalCollectionSyncState.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_remote_missing_cannot_create_collection_or_sync_state(self, get_collection):
        get_collection.return_value = None
        preview = self.client.get(self.preview_url)
        self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(response.data["code"], "sync_action_not_allowed")
        self.assertFalse(ExternalCollectionSyncState.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_apply_reads_remote_once_outside_transaction(self, get_collection):
        def fetch(*_args, **_kwargs):
            self.assertFalse(connection.in_atomic_block)
            return remote_collection(watch_status="completed")

        get_collection.side_effect = fetch
        preview = self.client.get(self.preview_url)
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(get_collection.call_count, 2)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_apply_does_not_touch_watch_history_or_metadata_fields(self, get_collection):
        history, _created = add_history(
            user=self.user,
            entry=self.entry,
            record={"watched_on": "2026-08-09", "watched_label": "2026年8月9日"},
        )
        before = {
            "title": self.entry.title,
            "metadata": deepcopy(self.identity.metadata),
            "history": list(WatchHistoryRecord.objects.filter(pk=history.pk).values()),
        }
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.entry.refresh_from_db()
        self.identity.refresh_from_db()
        self.assertEqual(self.entry.title, before["title"])
        self.assertEqual(self.identity.metadata, before["metadata"])
        self.assertEqual(list(WatchHistoryRecord.objects.filter(pk=history.pk).values()), before["history"])


@override_settings(REST_FRAMEWORK=RELAXED_SYNC_THROTTLES)
class SyncConfirmationProtectionTests(APITransactionTestCase):
    reset_sequences = True
    setUp = ManualCollectionPullTests.setUp
    preview = ManualCollectionPullTests.preview
    apply = ManualCollectionPullTests.apply

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_token_contains_only_context_and_fingerprints(self, get_collection):
        preview = self.preview(get_collection)
        payload = decode_preview_token(preview.data["preview_token"])
        self.assertEqual(set(payload), CONFIRMATION_KEYS)
        encoded = str(payload)
        for forbidden in (
            "test-access-token",
            "本地短评",
            "远端短评",
            "watching",
            "8.5",
            "credential_ciphertext",
            "raw",
        ):
            self.assertNotIn(forbidden, encoded)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_malformed_tampered_and_expired_tokens_have_stable_errors(self, get_collection):
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))
        token = preview.data["preview_token"]
        for candidate in ("not-signed", token[:-1] + ("a" if token[-1] != "a" else "b")):
            with self.subTest(candidate=candidate[:12]):
                response = self.apply(candidate, [{"field": "watch_status", "action": "pull_remote"}])
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
                self.assertEqual(response.data["code"], "sync_preview_invalid")

        with override_settings(EXTERNAL_SYNC_CONFIRMATION_MAX_AGE_SECONDS=-1):
            expired = self.apply(token, [{"field": "watch_status", "action": "pull_remote"}])
        self.assertEqual(expired.status_code, status.HTTP_400_BAD_REQUEST, expired.data)
        self.assertEqual(expired.data["code"], "sync_preview_expired")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_token_is_bound_to_user_provider_and_entry(self, get_collection):
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))
        token = preview.data["preview_token"]

        self.client.force_authenticate(self.other)
        wrong_user = self.apply(token, [{"field": "watch_status", "action": "pull_remote"}])
        self.assertEqual(wrong_user.status_code, status.HTTP_400_BAD_REQUEST, wrong_user.data)
        self.assertEqual(wrong_user.data["code"], "sync_preview_invalid")
        self.client.force_authenticate(self.user)

        wrong_provider_url = reverse(
            "external-collection-sync-apply",
            kwargs={"provider": "other", "entry_id": self.entry.pk},
        )
        wrong_provider = self.client.post(
            wrong_provider_url,
            {"preview_token": token, "actions": [{"field": "watch_status", "action": "pull_remote"}]},
            format="json",
        )
        self.assertEqual(wrong_provider.status_code, status.HTTP_400_BAD_REQUEST, wrong_provider.data)
        self.assertEqual(wrong_provider.data["code"], "sync_preview_invalid")

        another = JournalEntry.objects.create(user=self.user, title="另一个作品")
        wrong_entry_url = reverse(
            "external-collection-sync-apply",
            kwargs={"provider": "bangumi", "entry_id": another.pk},
        )
        wrong_entry = self.client.post(
            wrong_entry_url,
            {"preview_token": token, "actions": [{"field": "watch_status", "action": "pull_remote"}]},
            format="json",
        )
        self.assertEqual(wrong_entry.status_code, status.HTTP_400_BAD_REQUEST, wrong_entry.data)
        self.assertEqual(wrong_entry.data["code"], "sync_preview_invalid")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_local_remote_and_baseline_changes_each_make_preview_stale(self, get_collection):
        scenarios = ("local", "remote", "baseline")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                ExternalCollectionSyncState.objects.all().delete()
                JournalEntry.objects.filter(pk=self.entry.pk).update(watch_status="watching", review="本地短评")
                get_collection.reset_mock()
                get_collection.return_value = remote_collection(watch_status="completed")
                preview = self.client.get(self.preview_url)
                self.assertEqual(preview.status_code, status.HTTP_200_OK, preview.data)
                if scenario == "local":
                    JournalEntry.objects.filter(pk=self.entry.pk).update(review="并发本地修改")
                elif scenario == "remote":
                    get_collection.return_value = remote_collection(watch_status="dropped")
                else:
                    ExternalCollectionSyncState.objects.create(
                        identity=self.identity,
                        connection=self.account_connection,
                        baselines={"review": {"present": True, "value": "并发基线"}},
                    )
                response = self.apply(
                    preview.data["preview_token"],
                    [{"field": "watch_status", "action": "pull_remote"}],
                )
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
                self.assertEqual(response.data["code"], "sync_preview_stale")
                self.entry.refresh_from_db()
                expected_status = "watching"
                self.assertEqual(self.entry.watch_status, expected_status)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_identity_and_connection_replacement_are_context_changes(self, get_collection):
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))
        ExternalMediaIdentity.objects.filter(pk=self.identity.pk).update(external_id="456")
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "sync_context_changed")

        ExternalMediaIdentity.objects.filter(pk=self.identity.pk).update(external_id="1424")
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))
        self.account_connection.delete()
        UserExternalAccountConnection.objects.create(
            user=self.user,
            provider="bangumi",
            auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
            external_user_id="100",
            external_username="apply-owner",
            credential_ciphertext=encrypt_credentials(
                {"access_token": "replacement-token", "token_type": "Bearer"}
            ),
            connected_at=timezone.now(),
        )
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "sync_context_changed")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_identity_rebind_during_fresh_remote_get_is_rejected(self, get_collection):
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))

        def rebind(*_args, **_kwargs):
            ExternalMediaIdentity.objects.filter(pk=self.identity.pk).update(external_id="456")
            return remote_collection(watch_status="completed")

        get_collection.side_effect = rebind
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "sync_context_changed")
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.watch_status, "watching")
        self.assertFalse(ExternalCollectionSyncState.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_needs_reauthorization_blocks_apply_without_deleting_baseline(self, get_collection):
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.account_connection,
            baselines={"watch_status": {"present": True, "value": "planned"}},
        )
        before = deepcopy(state.baselines)
        preview = self.preview(get_collection, remote_collection(watch_status="completed"))
        self.account_connection.status = UserExternalAccountConnection.Status.NEEDS_REAUTHORIZATION
        self.account_connection.save(update_fields=["status", "updated_at"])
        response = self.apply(
            preview.data["preview_token"],
            [{"field": "watch_status", "action": "pull_remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "external_account_needs_reauthorization")
        state.refresh_from_db()
        self.assertEqual(state.baselines, before)
