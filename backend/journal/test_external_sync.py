from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITransactionTestCase

from journal.external_accounts.credentials import encrypt_credentials
from journal.external_accounts.errors import account_token_invalid
from journal.external_accounts.providers.bangumi import BangumiAccountProvider
from journal.external_media.services import refresh_external_identity
from journal.external_sync.canonical import MISSING, canonical_snapshot, fingerprint
from journal.external_sync.planner import plan_collection
from journal.models import (
    ExternalCollectionSyncState,
    ExternalMediaIdentity,
    JournalEntry,
    UserExternalAccountConnection,
    WatchHistoryRecord,
)
from journal.watch_history import add_history

User = get_user_model()


def capabilities(local=None):
    return {
        field: {"supported": True, "reason": None}
        for field in ("watch_status", "personal_score", "review")
    }


def field_plan(result, field):
    return next(item for item in result["fields"] if item["field"] == field)


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


class ThreeWayPlannerTests(APITransactionTestCase):
    reset_sequences = True

    def test_all_three_way_states_are_computed_for_every_supported_field(self):
        samples = {
            "watch_status": ("planned", "watching", "completed"),
            "personal_score": ("7", "8", "9"),
            "review": ("base", "local", "remote"),
        }
        cases = {
            "in_sync": lambda base, local, remote: (base, base, base),
            "local_changed": lambda base, local, remote: (base, local, base),
            "remote_changed": lambda base, local, remote: (base, base, remote),
            "converged": lambda base, local, remote: (base, local, local),
            "conflict": lambda base, local, remote: (base, local, remote),
        }
        defaults = canonical_snapshot(watch_status="planned", personal_score=7, review="base")
        for field, (base, local, remote) in samples.items():
            for expected, values in cases.items():
                baseline_value, local_value, remote_value = values(base, local, remote)
                baseline = dict(defaults)
                local_snapshot = dict(defaults)
                remote_snapshot = dict(defaults)
                baseline[field] = {"present": True, "value": baseline_value}
                local_snapshot[field] = {"present": True, "value": local_value}
                remote_snapshot[field] = {"present": True, "value": remote_value}
                result = plan_collection(
                    baseline=baseline,
                    local=local_snapshot,
                    remote=remote_snapshot,
                    push_capabilities=capabilities(),
                )
                self.assertEqual(field_plan(result, field)["state"], expected, (field, expected))

    def test_uninitialized_equal_uninitialized_and_remote_missing_are_distinct(self):
        local = canonical_snapshot(watch_status="watching", personal_score=MISSING, review="")
        equal = plan_collection(
            baseline={}, local=local, remote=local, push_capabilities=capabilities()
        )
        self.assertTrue(all(item["state"] == "uninitialized_equal" for item in equal["fields"]))

        remote = canonical_snapshot(watch_status="completed", personal_score=8, review=MISSING)
        unequal = plan_collection(
            baseline={}, local=local, remote=remote, push_capabilities=capabilities()
        )
        self.assertTrue(all(item["state"] == "uninitialized" for item in unequal["fields"]))

        missing = plan_collection(
            baseline={}, local=local, remote={}, remote_missing=True, push_capabilities=capabilities()
        )
        self.assertTrue(all(item["state"] == "remote_missing" for item in missing["fields"]))
        self.assertTrue(all(not item["pull_supported"] for item in missing["fields"]))

    def test_missing_empty_and_decimal_semantics_are_stable(self):
        score_missing = canonical_snapshot(watch_status="watching", personal_score=MISSING, review=MISSING)
        score_zero = canonical_snapshot(watch_status="watching", personal_score=0, review="")
        self.assertNotEqual(score_missing["personal_score"], score_zero["personal_score"])
        self.assertNotEqual(score_missing["review"], score_zero["review"])
        self.assertEqual(
            canonical_snapshot(watch_status="watching", personal_score=8, review="")["personal_score"],
            canonical_snapshot(watch_status="watching", personal_score=Decimal("8.0"), review="")["personal_score"],
        )
        self.assertNotEqual(
            fingerprint(score_missing),
            fingerprint(score_zero),
        )

    def test_bangumi_push_capabilities_never_round_fractional_score(self):
        fractional = canonical_snapshot(watch_status="watching", personal_score=Decimal("8.5"), review="a\nb")
        result = BangumiAccountProvider.collection_push_capabilities(fractional)
        self.assertFalse(result["personal_score"]["supported"])
        self.assertEqual(result["personal_score"]["reason"], "fractional_score_not_supported")
        self.assertEqual(fractional["personal_score"]["value"], "8.5")

        too_long = canonical_snapshot(watch_status="watching", personal_score=8, review="x" * 381)
        self.assertEqual(
            BangumiAccountProvider.collection_push_capabilities(too_long)["review"],
            {"supported": False, "reason": "review_exceeds_provider_limit"},
        )
        unsupported_character = canonical_snapshot(
            watch_status="watching", personal_score=8, review="visible\u0000hidden"
        )
        self.assertEqual(
            BangumiAccountProvider.collection_push_capabilities(unsupported_character)["review"],
            {"supported": False, "reason": "review_contains_unsupported_characters"},
        )
        self.assertTrue(
            BangumiAccountProvider.collection_push_capabilities(fractional)["review"]["supported"]
        )

        planned_score = canonical_snapshot(watch_status="planned", personal_score=8, review="")
        self.assertEqual(
            BangumiAccountProvider.collection_push_capabilities(planned_score)["personal_score"],
            {"supported": False, "reason": "planned_status_forces_score_clear"},
        )

    def test_all_statuses_and_remote_zero_are_canonicalized(self):
        provider = BangumiAccountProvider()
        for status_code, expected in ((1, "planned"), (2, "completed"), (3, "watching"), (4, "on_hold"), (5, "dropped")):
            normalized = provider.normalize_collection(
                {
                    "subject_id": 1424,
                    "subject_type": 2,
                    "type": status_code,
                    "rate": 0,
                    "comment": None,
                    "subject": {"id": 1424, "name": "K-ON!"},
                }
            )
            snapshot = provider.collection_sync_snapshot(normalized)
            self.assertEqual(snapshot["watch_status"], {"present": True, "value": expected})
            self.assertEqual(snapshot["personal_score"], {"present": False, "value": None})
            self.assertEqual(snapshot["review"], {"present": False, "value": None})


class ExternalCollectionSyncStateTests(APITransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(username="sync-owner", password="StrongPass123!")
        self.other = User.objects.create_user(username="sync-other", password="StrongPass123!")
        self.entry = JournalEntry.objects.create(user=self.user, title="同步作品")
        self.identity = ExternalMediaIdentity.objects.create(
            entry=self.entry,
            provider="bangumi",
            external_id="1424",
            canonical_url="https://bgm.tv/subject/1424",
        )
        self.connection = self.create_connection(self.user, "100", "sync-owner")

    @staticmethod
    def create_connection(user, external_user_id, username):
        return UserExternalAccountConnection.objects.create(
            user=user,
            provider="bangumi",
            auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
            external_user_id=external_user_id,
            external_username=username,
            credential_ciphertext=encrypt_credentials(
                {"access_token": "test-access-token", "token_type": "Bearer"}
            ),
            connected_at=timezone.now(),
        )

    def test_state_validates_schema_ownership_provider_and_cascades(self):
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.connection,
            baselines={
                "watch_status": {"present": True, "value": "planned"},
                "personal_score": {"present": False, "value": None},
                "review": {"present": True, "value": ""},
            },
        )
        other_connection = self.create_connection(self.other, "200", "sync-other")
        state.connection = other_connection
        with self.assertRaises(ValidationError):
            state.save()
        state.refresh_from_db()
        self.connection.delete()
        self.assertFalse(ExternalCollectionSyncState.objects.filter(pk=state.pk).exists())
        self.assertTrue(JournalEntry.objects.filter(pk=self.entry.pk).exists())
        self.assertTrue(ExternalMediaIdentity.objects.filter(pk=self.identity.pk).exists())

    def test_token_rotation_on_same_connection_preserves_baseline(self):
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.connection,
            baselines={"watch_status": {"present": True, "value": "watching"}},
        )
        self.connection.credential_ciphertext = encrypt_credentials(
            {"access_token": "rotated-access-token", "token_type": "Bearer"}
        )
        self.connection.save(update_fields=["credential_ciphertext", "updated_at"])

        state.refresh_from_db()
        self.assertEqual(state.connection_id, self.connection.pk)
        self.assertEqual(state.baselines["watch_status"]["value"], "watching")

    def test_invalid_or_unbounded_baseline_is_rejected(self):
        for baselines in (
            {"title": {"present": True, "value": "not-syncable"}},
            {"personal_score": {"present": True, "value": "8.50"}},
            {"review": {"present": True, "value": "x" * 10_001}},
        ):
            with self.subTest(baselines=list(baselines)):
                with self.assertRaises(ValidationError):
                    ExternalCollectionSyncState.objects.create(
                        identity=self.identity,
                        connection=self.connection,
                        baselines=baselines,
                    )

    def test_identity_delete_cascades_without_deleting_entry_or_connection(self):
        state = ExternalCollectionSyncState.objects.create(identity=self.identity, connection=self.connection)
        self.identity.delete()
        self.assertFalse(ExternalCollectionSyncState.objects.filter(pk=state.pk).exists())
        self.assertTrue(JournalEntry.objects.filter(pk=self.entry.pk).exists())
        self.assertTrue(UserExternalAccountConnection.objects.filter(pk=self.connection.pk).exists())


class ReadOnlySyncPreviewTests(APITransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(username="preview-owner", password="StrongPass123!")
        self.other = User.objects.create_user(username="preview-other", password="StrongPass123!")
        self.entry = JournalEntry.objects.create(
            user=self.user,
            title="同步预览作品",
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
        self.connection = UserExternalAccountConnection.objects.create(
            user=self.user,
            provider="bangumi",
            auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
            external_user_id="100",
            external_username="preview-owner",
            credential_ciphertext=encrypt_credentials(
                {"access_token": "test-access-token", "token_type": "Bearer"}
            ),
            connected_at=timezone.now(),
        )
        self.url = reverse(
            "external-collection-sync-preview",
            kwargs={"provider": "bangumi", "entry_id": self.entry.pk},
        )
        self.client.force_authenticate(self.user)

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_preview_is_server_authoritative_read_only_and_network_is_outside_atomic(self, get_collection):
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.connection,
            baselines=canonical_snapshot(watch_status="planned", personal_score=8, review="base"),
            last_synced_at=timezone.now(),
        )
        history, _created = add_history(
            user=self.user,
            entry=self.entry,
            record={"watched_on": "2026-08-01", "watched_label": "2026年8月1日"},
        )
        original_entry = (self.entry.watch_status, str(self.entry.personal_score), self.entry.review)
        original_baseline = dict(state.baselines)
        original_history = list(
            WatchHistoryRecord.objects.filter(pk=history.pk).values("watched_on", "watched_label", "sequence")
        )

        def fetch(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return remote_collection()

        get_collection.side_effect = fetch
        response = self.client.get(
            self.url,
            {"local": "forged", "remote": "forged", "baseline": "forged"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["provider"], "bangumi")
        self.assertFalse(any("token" in str(key).lower() for key in response.data))
        score = field_plan(response.data, "personal_score")
        self.assertEqual(score["local"], {"present": True, "value": "8.5"})
        self.assertFalse(score["push_supported"])
        self.assertEqual(score["push_block_reason"], "fractional_score_not_supported")

        self.entry.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual((self.entry.watch_status, str(self.entry.personal_score), self.entry.review), original_entry)
        self.assertEqual(state.baselines, original_baseline)
        self.assertEqual(
            list(WatchHistoryRecord.objects.filter(pk=history.pk).values("watched_on", "watched_label", "sequence")),
            original_history,
        )

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_preview_does_not_create_baseline_even_when_equal(self, get_collection):
        self.entry.personal_score = 8
        self.entry.review = "远端短评"
        self.entry.save(update_fields=["personal_score", "review", "updated_at"])
        get_collection.return_value = remote_collection()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(all(item["state"] == "uninitialized_equal" for item in response.data["fields"]))
        self.assertFalse(ExternalCollectionSyncState.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_remote_missing_is_returned_without_creating_or_mutating(self, get_collection):
        get_collection.return_value = None
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(all(item["state"] == "remote_missing" for item in response.data["fields"]))
        self.assertFalse(ExternalCollectionSyncState.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_stale_identity_response_is_rejected(self, get_collection):
        def rebind(*args, **kwargs):
            self.identity.external_id = "456"
            self.identity.canonical_url = "https://bgm.tv/subject/456"
            self.identity.save(update_fields=["external_id", "canonical_url", "updated_at"])
            return remote_collection(external_id="1424")

        get_collection.side_effect = rebind
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "sync_context_changed")

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_disconnect_reconnect_response_is_rejected_and_old_baseline_is_deleted(self, get_collection):
        ExternalCollectionSyncState.objects.create(identity=self.identity, connection=self.connection)

        def reconnect(*args, **kwargs):
            self.connection.delete()
            UserExternalAccountConnection.objects.create(
                user=self.user,
                provider="bangumi",
                auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
                external_user_id="100",
                external_username="preview-owner",
                credential_ciphertext=encrypt_credentials(
                    {"access_token": "replacement-token", "token_type": "Bearer"}
                ),
                connected_at=timezone.now(),
            )
            return remote_collection()

        get_collection.side_effect = reconnect
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "sync_context_changed")
        self.assertFalse(ExternalCollectionSyncState.objects.exists())

    @patch("journal.external_accounts.providers.bangumi.BangumiAccountProvider.get_collection")
    def test_provider_auth_failure_marks_and_reports_reauthorization(self, get_collection):
        get_collection.side_effect = account_token_invalid()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["code"], "external_account_needs_reauthorization")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, UserExternalAccountConnection.Status.NEEDS_REAUTHORIZATION)

    def test_cross_user_wrong_provider_and_needs_reauth_are_denied(self):
        self.client.force_authenticate(self.other)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data["code"], "sync_target_not_found")

        self.client.force_authenticate(self.user)
        wrong = self.client.get(
            reverse(
                "external-collection-sync-preview",
                kwargs={"provider": "unknown", "entry_id": self.entry.pk},
            )
        )
        self.assertEqual(wrong.status_code, status.HTTP_400_BAD_REQUEST)
        self.connection.status = UserExternalAccountConnection.Status.NEEDS_REAUTHORIZATION
        self.connection.save(update_fields=["status", "updated_at"])
        denied = self.client.get(self.url)
        self.assertEqual(denied.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(denied.data["code"], "external_account_needs_reauthorization")

    @patch("journal.external_media.services.get_provider")
    def test_metadata_refresh_does_not_change_collection_baseline(self, get_provider):
        self.identity.is_metadata_source = True
        self.identity.save(update_fields=["is_metadata_source", "updated_at"])
        state = ExternalCollectionSyncState.objects.create(
            identity=self.identity,
            connection=self.connection,
            baselines=canonical_snapshot(watch_status="planned", personal_score=8, review="base"),
        )
        provider = get_provider.return_value
        provider.slug = "bangumi"
        provider.refresh.return_value = {
            "japanese_title": "更新后的资料标题",
            "air_date": "2026-08-09",
            "studio": "更新后的制作方",
            "episodes": 12,
            "poster_url": "",
        }
        provider.canonical_url.return_value = "https://bgm.tv/subject/1424"
        original = dict(state.baselines)

        refresh_external_identity(entry=self.entry, user=self.user, provider_slug="bangumi")

        state.refresh_from_db()
        self.assertEqual(state.baselines, original)
        self.assertIsNone(state.last_synced_at)
