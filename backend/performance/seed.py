"""Deterministic generated data for isolated backend performance probes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from accounts.models import StaffProfile, UserSecurityProfile
from integrations.models import (
    ExternalIdentityBinding,
    IntegrationConnection,
    IntegrationEvent,
)
from journal.models import (
    ExternalMediaIdentity,
    JournalEntry,
    UserSettings,
    WatchHistoryRecord,
)
from journal.watch_history.validation import semantic_digest_from_values
from plugin_host.models import (
    PluginDeployment,
    PluginPackageBlob,
    PluginProject,
    PluginSubmission,
    PluginVersion,
    UserPluginInstallation,
)
from site_config.models import InstallationState

from .contract import DATASETS

NAMESPACE = "perf-v1"
OWNER_USERNAME = f"{NAMESPACE}-owner"
ADMIN_USERNAME = f"{NAMESPACE}-admin"
USER_PREFIX = f"{NAMESPACE}-user-"
LOAD_ENTRY_PREFIX = "Anime Load Journey"
LOAD_ENTRIES_PER_USER = 50
PLUGIN_PREFIX = f"{NAMESPACE}-plugin-"
PLUGIN_STORAGE_PREFIX = f"{NAMESPACE}/packages/"
INTEGRATION_PROVIDER = NAMESPACE
INTEGRATION_SECRET = "perf-v1-ephemeral-secret"


@dataclass(frozen=True)
class SeedResult:
    dataset: str
    owner_id: int
    admin_id: int
    journal_entries: int
    supporting_users: int
    plugins: int
    watch_history_records: int
    integration_events: int
    history_entry_id: int
    detail_entry_id: int
    integration_connection_id: str


@dataclass(frozen=True)
class LoadUserJourney:
    username: str
    entry_id: int


def dataset_shape(name):
    try:
        return DATASETS[str(name).lower()]
    except KeyError as error:
        raise ValueError(f"unknown performance dataset: {name}") from error


def reset_backend_performance_data():
    """Delete only records owned by this generated-data namespace."""

    IntegrationConnection.objects.filter(provider=INTEGRATION_PROVIDER).delete()
    PluginDeployment.objects.filter(plugin__slug__startswith=PLUGIN_PREFIX).delete()
    PluginProject.objects.filter(slug__startswith=PLUGIN_PREFIX).delete()
    PluginPackageBlob.objects.filter(storage_path__startswith=PLUGIN_STORAGE_PREFIX).delete()
    get_user_model().objects.filter(username__startswith=f"{NAMESPACE}-").delete()


@transaction.atomic
def provision_load_user_journeys(count, *, entries_per_user=LOAD_ENTRIES_PER_USER):
    """Give each isolated virtual user an owned, read-only Journey fixture."""

    count = int(count)
    entries_per_user = int(entries_per_user)
    if count <= 0 or entries_per_user <= 0:
        raise ValueError("load identity and entry counts must be positive")

    user_model = get_user_model()
    supporting_users = list(
        user_model.objects.filter(username__startswith=USER_PREFIX).order_by("username")
    )
    users = supporting_users[:count]
    if len(users) != count:
        raise ValueError(f"requested {count} load identities but only {len(users)} are seeded")

    JournalEntry.objects.filter(user__in=users, title__startswith=LOAD_ENTRY_PREFIX).delete()
    statuses = [choice for choice, _label in JournalEntry.WatchStatus.choices]
    entries = []
    for user_index, user in enumerate(users):
        for entry_index in range(entries_per_user):
            entries.append(
                JournalEntry(
                    user=user,
                    title=f"{LOAD_ENTRY_PREFIX} {user_index:02d}-{entry_index:03d} anime",
                    japanese_title=f"負荷測定 {user_index:02d}-{entry_index:03d}",
                    airing_period=f"{2000 + entry_index % 27}-01",
                    studio=f"Load Studio {entry_index % 10:02d}",
                    episodes=str(12 + entry_index % 14),
                    description="Isolated virtual-user read fixture",
                    tags=["anime", f"load-{entry_index % 5}"],
                    tag_colors={"anime": "#4ecdc4"},
                    personal_score=f"{6 + entry_index % 40 / 10:.2f}",
                    watch_status=statuses[entry_index % len(statuses)],
                    review="isolated load journey",
                    visibility=JournalEntry.Visibility.PRIVATE,
                )
            )
    JournalEntry.objects.bulk_create(entries, batch_size=500)

    first_entries = {}
    for entry in JournalEntry.objects.filter(
        user__in=users,
        title__startswith=LOAD_ENTRY_PREFIX,
    ).order_by("user_id", "id"):
        first_entries.setdefault(entry.user_id, entry)

    watched_on = date(2026, 1, 1)
    WatchHistoryRecord.objects.bulk_create(
        [
            WatchHistoryRecord(
                entry=first_entries[user.pk],
                watched_on=watched_on,
                watched_label=watched_on.isoformat(),
                brush_number=1,
                brush_label="負荷測定",
                episode_start=1,
                episode_end=1,
                notes=["isolated-load"],
                metadata={"source": "performance-load"},
                sequence=1,
                semantic_key=semantic_digest_from_values(watched_on, "負荷測定", 1, 1),
            )
            for user in users
        ],
        batch_size=100,
    )
    return tuple(
        LoadUserJourney(username=user.username, entry_id=first_entries[user.pk].pk)
        for user in users
    )


def _manifest(index):
    return {
        "schemaVersion": 2,
        "sdkApi": 2,
        "id": f"cc.animemo.perf.{index:03d}",
        "slug": f"{PLUGIN_PREFIX}{index:03d}",
        "name": f"Performance Plugin {index:03d}",
        "version": "1.0.0",
        "description": "Deterministic performance fixture",
        "author": {"name": "AniMemo Performance"},
        "license": "MIT",
        "installationMode": "user",
        "runtimes": ["frontend"],
        "extensions": ["dashboard.card"],
        "permissions": [],
        "hooks": [],
        "settings": [{"key": "compact", "scope": "user", "type": "boolean"}],
        "dataPolicy": {
            "storesPersonalData": False,
            "usesExternalNetwork": False,
            "acceptsFileUploads": False,
            "retainsDataOnDisable": True,
        },
    }


@transaction.atomic
def seed_backend_performance_data(dataset, *, reset=True):
    """Generate one exact SMALL/MEDIUM/LARGE dataset without fixture files."""

    shape = dataset_shape(dataset)
    if reset:
        reset_backend_performance_data()

    user_model = get_user_model()
    owner = user_model(username=OWNER_USERNAME, email=f"{OWNER_USERNAME}@example.test")
    owner.set_unusable_password()
    owner.save()
    admin = user_model(
        username=ADMIN_USERNAME,
        email=f"{ADMIN_USERNAME}@example.test",
        is_staff=True,
        is_superuser=True,
    )
    admin.set_unusable_password()
    admin.save()

    installation = InstallationState.load()
    if installation.status != InstallationState.Status.INITIALIZED:
        installation.status = InstallationState.Status.INITIALIZED
        installation.initialized_at = timezone.now()
        installation.initialized_by = admin
        installation.save(
            update_fields=[
                "status",
                "initialized_at",
                "initialized_by",
                "updated_at",
            ]
        )

    supporting_users = []
    for index in range(shape.supporting_users):
        user = user_model(
            username=f"{USER_PREFIX}{index:04d}",
            email=f"{USER_PREFIX}{index:04d}@example.test",
            is_staff=index % 10 == 0,
        )
        user.set_unusable_password()
        supporting_users.append(user)
    user_model.objects.bulk_create(supporting_users, batch_size=500)
    supporting_users = list(
        user_model.objects.filter(username__startswith=USER_PREFIX).order_by("username")
    )

    all_users = [owner, admin, *supporting_users]
    UserSecurityProfile.objects.bulk_create(
        [
            UserSecurityProfile(
                user=user,
                email_verified=index % 3 != 0,
                two_factor_enabled=user.is_staff and index % 2 == 0,
            )
            for index, user in enumerate(all_users)
        ],
        batch_size=500,
    )
    StaffProfile.objects.bulk_create(
        [
            StaffProfile(user=user, role=StaffProfile.Role.REVIEWER)
            for user in supporting_users
            if user.is_staff
        ],
        batch_size=500,
    )
    UserSettings.objects.bulk_create(
        [
            UserSettings(
                user=user,
                nickname=f"Performance User {index:04d}",
                public_status=(
                    UserSettings.PublicStatus.PENDING
                    if index % 11 == 0
                    else UserSettings.PublicStatus.PRIVATE
                ),
            )
            for index, user in enumerate(all_users)
        ],
        batch_size=500,
    )

    statuses = [choice for choice, _label in JournalEntry.WatchStatus.choices]
    tag_pool = ["科幻", "日常", "冒险", "治愈", "校园", "悬疑", "原创", "续作"]
    entries = []
    for index in range(shape.journal_entries):
        score = None if index % 9 == 0 else f"{(index % 101) / 10:.2f}"
        entries.append(
            JournalEntry(
                user=owner,
                title=f"Performance Entry {index:05d}",
                japanese_title=f"性能作品 {index:05d}",
                airing_period=f"{2000 + index % 27}-01",
                studio=f"Studio {index % 20:02d}",
                episodes=str(12 + index % 14),
                description=f"Deterministic performance fixture {index}",
                tags=[tag_pool[index % len(tag_pool)], tag_pool[(index + 3) % len(tag_pool)]],
                tag_colors={tag_pool[index % len(tag_pool)]: "#4ecdc4"},
                personal_score=score,
                watch_status=statuses[index % len(statuses)],
                review="baseline " + ("x" * (index % 80)),
                visibility=(
                    JournalEntry.Visibility.PUBLIC
                    if index % 20 == 0
                    else JournalEntry.Visibility.PRIVATE
                ),
            )
        )
    JournalEntry.objects.bulk_create(entries, batch_size=500)
    entries = list(
        JournalEntry.objects.filter(user=owner).order_by("id").only("id", "user_id")
    )

    identities = []
    for index, entry in enumerate(entries[::5]):
        identities.append(
            ExternalMediaIdentity(
                entry=entry,
                provider="bangumi",
                external_id=f"perf-{index:06d}",
                canonical_url=f"https://bgm.tv/subject/{900000 + index}",
                metadata={"title": f"Provider title {index}", "score": round(6 + index % 40 / 10, 1)},
                is_metadata_source=True,
                metadata_fetched_at=timezone.now(),
            )
        )
    ExternalMediaIdentity.objects.bulk_create(identities, batch_size=500)

    history_rows = []
    history_target_count = min(500, shape.watch_history_records)
    history_entry = entries[0]
    start_date = date(2024, 1, 1)
    for index in range(history_target_count):
        watched_on = start_date + timedelta(days=index % 730)
        episode = index + 1
        brush_label = f"基线 {index + 1}"
        history_rows.append(
            WatchHistoryRecord(
                entry=history_entry,
                watched_on=watched_on,
                watched_label=watched_on.isoformat(),
                brush_number=(index % 5) + 1,
                brush_label=brush_label,
                episode_start=episode,
                episode_end=episode,
                notes=[f"fixture-{index % 7}"],
                metadata={"source": "performance-seed", "bucket": index % 10},
                sequence=index + 1,
                semantic_key=semantic_digest_from_values(watched_on, brush_label, episode, episode),
            )
        )
    remaining = shape.watch_history_records - history_target_count
    for index in range(remaining):
        entry = entries[1 + index % max(1, len(entries) - 1)]
        sequence = index // max(1, len(entries) - 1) + 1
        watched_on = start_date + timedelta(days=index % 730)
        episode = sequence
        brush_label = f"补充 {sequence}"
        history_rows.append(
            WatchHistoryRecord(
                entry=entry,
                watched_on=watched_on,
                watched_label=watched_on.isoformat(),
                brush_number=1,
                brush_label=brush_label,
                episode_start=episode,
                episode_end=episode,
                notes=[],
                metadata={"source": "performance-seed"},
                sequence=sequence,
                semantic_key=semantic_digest_from_values(watched_on, brush_label, episode, episode),
            )
        )
    WatchHistoryRecord.objects.bulk_create(history_rows, batch_size=500)

    plugin_projects = [
        PluginProject(
            plugin_id=f"cc.animemo.perf.{index:03d}",
            slug=f"{PLUGIN_PREFIX}{index:03d}",
            name=f"Performance Plugin {index:03d}",
            description="Deterministic performance fixture",
            owner=admin,
            installation_mode=PluginProject.InstallationMode.USER,
        )
        for index in range(shape.plugins)
    ]
    PluginProject.objects.bulk_create(plugin_projects, batch_size=200)
    plugin_projects = list(
        PluginProject.objects.filter(slug__startswith=PLUGIN_PREFIX).order_by("slug")
    )
    blobs = []
    for index in range(shape.plugins):
        digest = hashlib.sha256(f"{NAMESPACE}-package-{index}".encode()).hexdigest()
        blobs.append(
            PluginPackageBlob(
                sha256=digest,
                size_bytes=4096 + index * 37,
                storage_path=f"{PLUGIN_STORAGE_PREFIX}{digest}.zip",
            )
        )
    PluginPackageBlob.objects.bulk_create(blobs, batch_size=200)
    blobs = list(
        PluginPackageBlob.objects.filter(storage_path__startswith=PLUGIN_STORAGE_PREFIX).order_by("storage_path")
    )
    blob_by_digest = {blob.sha256: blob for blob in blobs}
    versions = []
    for index, project in enumerate(plugin_projects):
        digest = hashlib.sha256(f"{NAMESPACE}-package-{index}".encode()).hexdigest()
        manifest = _manifest(index)
        versions.append(
            PluginVersion(
                plugin=project,
                version="1.0.0",
                package_blob=blob_by_digest[digest],
                manifest_snapshot=manifest,
                runtime_types=["frontend"],
                review_status=PluginVersion.ReviewStatus.APPROVED,
                published_at=timezone.now(),
                created_by=admin,
            )
        )
    PluginVersion.objects.bulk_create(versions, batch_size=200)
    versions = list(
        PluginVersion.objects.filter(plugin__slug__startswith=PLUGIN_PREFIX).select_related("plugin").order_by("plugin__slug")
    )
    PluginDeployment.objects.bulk_create(
        [
            PluginDeployment(
                plugin=version.plugin,
                current_version=version,
                enabled=True,
                healthy=True,
                status=PluginDeployment.Status.ENABLED,
                disk_bytes=version.package_blob.size_bytes,
                updated_by=admin,
            )
            for version in versions
        ],
        batch_size=200,
    )
    PluginSubmission.objects.bulk_create(
        [
            PluginSubmission(
                plugin_version=version,
                submitter=admin,
                status=PluginSubmission.Status.APPROVED,
                reviewer=admin,
                reviewed_at=timezone.now(),
                security_report={"status": "pass", "fixture": True},
            )
            for version in versions
        ],
        batch_size=200,
    )
    installation_users = [owner, *supporting_users[: min(20, len(supporting_users))]]
    UserPluginInstallation.objects.bulk_create(
        [
            UserPluginInstallation(
                user=user,
                plugin=project,
                enabled=(user.pk + project.pk) % 7 != 0,
                config={"compact": (user.pk + project.pk) % 2 == 0},
            )
            for project in plugin_projects
            for user in installation_users
        ],
        batch_size=500,
    )

    connections = []
    for index in range(2):
        connection = IntegrationConnection(
            provider=INTEGRATION_PROVIDER,
            instance_id=f"instance-{index}",
            name=f"Performance Integration {index}",
            key_id=f"{NAMESPACE}-key-{index}",
        )
        connection.set_secret(INTEGRATION_SECRET)
        connections.append(connection)
    IntegrationConnection.objects.bulk_create(connections)
    connections = list(
        IntegrationConnection.objects.filter(provider=INTEGRATION_PROVIDER).order_by("instance_id")
    )
    ExternalIdentityBinding.objects.bulk_create(
        [
            ExternalIdentityBinding(
                connection=connections[index % len(connections)],
                user=user,
                platform="qq",
                external_user_id=f"perf-user-{index:05d}",
                display_name=user.username,
                enabled=True,
                verified_at=timezone.now(),
            )
            for index, user in enumerate([owner, *supporting_users])
        ],
        batch_size=500,
    )
    integration_event_count = min(shape.journal_entries, 1000)
    IntegrationEvent.objects.bulk_create(
        [
            IntegrationEvent(
                connection=connections[0],
                user=owner,
                platform="qq",
                external_user_id="perf-user-00000",
                plugin_slug=plugin_projects[index % len(plugin_projects)].slug,
                event_name="journal-updated",
                payload={"entry_id": entries[index % len(entries)].pk, "index": index},
            )
            for index in range(integration_event_count)
        ],
        batch_size=500,
    )

    return SeedResult(
        dataset=shape.name,
        owner_id=owner.pk,
        admin_id=admin.pk,
        journal_entries=shape.journal_entries,
        supporting_users=shape.supporting_users,
        plugins=shape.plugins,
        watch_history_records=shape.watch_history_records,
        integration_events=integration_event_count,
        history_entry_id=history_entry.pk,
        detail_entry_id=entries[len(entries) // 2].pk,
        integration_connection_id=str(connections[0].pk),
    )
