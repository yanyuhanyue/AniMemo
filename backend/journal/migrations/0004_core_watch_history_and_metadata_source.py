import hashlib
import json
from datetime import date, datetime

from django.db import migrations, models
from django.db.models import F, Q


MAX_RECORDS = 500
MAX_INTEGER = 32767
MAX_NOTES = 20
MAX_NOTE_LENGTH = 500
MAX_METADATA_BYTES = 4096
CORE_FIELDS = {
    "id",
    "watched_on",
    "watched_label",
    "brush_number",
    "brush_label",
    "episode_start",
    "episode_end",
    "notes",
    "metadata",
    "sequence",
    "semantic_key",
    "created_at",
    "updated_at",
}


def _fail(row, detail):
    raise RuntimeError(f"watch_history PluginData row {row.pk} cannot be migrated safely: {detail}")


def _date(value, row, index):
    if isinstance(value, datetime):
        _fail(row, f"record {index + 1} watched_on is a datetime")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        _fail(row, f"record {index + 1} watched_on is missing")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        _fail(row, f"record {index + 1} watched_on is invalid")


def _positive(value, field, row, index):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        _fail(row, f"record {index + 1} {field} is invalid")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and value.strip().isdigit():
        normalized = int(value.strip())
    else:
        _fail(row, f"record {index + 1} {field} is invalid")
    if not 1 <= normalized <= MAX_INTEGER:
        _fail(row, f"record {index + 1} {field} is out of range")
    return normalized


def _normalize(record, row, index):
    if not isinstance(record, dict):
        _fail(row, f"record {index + 1} is not an object")
    watched_on = _date(record.get("watched_on"), row, index)
    brush_number = _positive(record.get("brush_number"), "brush_number", row, index)
    episode_start = _positive(record.get("episode_start"), "episode_start", row, index)
    episode_end = _positive(record.get("episode_end"), "episode_end", row, index)
    if episode_start is not None and episode_end is not None and episode_end < episode_start:
        _fail(row, f"record {index + 1} episode range is invalid")

    brush_label = str(record.get("brush_label") or "首刷").strip() or "首刷"
    watched_label = str(record.get("watched_label") or "").strip()
    if not watched_label:
        watched_label = f"{watched_on.year}年{watched_on.month}月{watched_on.day}日"
    if len(brush_label) > 20 or len(watched_label) > 80:
        _fail(row, f"record {index + 1} label exceeds the core schema")

    notes = record.get("notes", [])
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        _fail(row, f"record {index + 1} notes are invalid")
    notes = [note.strip() for note in notes if note.strip()]
    if len(notes) > MAX_NOTES or any(len(note) > MAX_NOTE_LENGTH for note in notes):
        _fail(row, f"record {index + 1} notes exceed the core schema")

    explicit_metadata = record.get("metadata") or {}
    if not isinstance(explicit_metadata, dict):
        _fail(row, f"record {index + 1} metadata is not an object")
    metadata = {
        **{key: value for key, value in record.items() if key not in CORE_FIELDS},
        **explicit_metadata,
    }
    try:
        encoded_metadata = json.dumps(
            metadata,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail(row, f"record {index + 1} metadata is not JSON serializable")
    if len(encoded_metadata) > MAX_METADATA_BYTES:
        _fail(row, f"record {index + 1} metadata exceeds {MAX_METADATA_BYTES} bytes")

    canonical = json.dumps(
        [watched_on.isoformat(), brush_label, episode_start, episode_end],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return {
        "watched_on": watched_on,
        "watched_label": watched_label,
        "brush_number": brush_number,
        "brush_label": brush_label,
        "episode_start": episode_start,
        "episode_end": episode_end,
        "notes": notes,
        "metadata": metadata,
        "semantic_key": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def migrate_watch_history(apps, schema_editor):
    PluginData = apps.get_model("plugin_host", "PluginData")
    JournalEntry = apps.get_model("journal", "JournalEntry")
    WatchHistoryRecord = apps.get_model("journal", "WatchHistoryRecord")

    rows = PluginData.objects.filter(
        plugin__slug="watch-history-importer",
        namespace="watch_history",
    ).order_by("pk")
    for row in rows.iterator():
        if row.user_id is None:
            _fail(row, "user is missing")
        try:
            entry_id = int(row.key)
        except (TypeError, ValueError):
            _fail(row, "key is not a JournalEntry id")
        entry = JournalEntry.objects.filter(pk=entry_id, user_id=row.user_id).first()
        if entry is None:
            _fail(row, "entry is missing or belongs to another user")
        if not isinstance(row.value, list) or len(row.value) > MAX_RECORDS:
            _fail(row, "value is not a bounded record list")

        normalized_by_key = {}
        for index, record in enumerate(row.value):
            normalized = _normalize(record, row, index)
            normalized_by_key[normalized["semantic_key"]] = normalized
        WatchHistoryRecord.objects.bulk_create(
            [
                WatchHistoryRecord(entry_id=entry_id, sequence=index + 1, **record)
                for index, record in enumerate(normalized_by_key.values())
            ]
        )
        row.delete()


def assign_metadata_sources(apps, schema_editor):
    ExternalMediaIdentity = apps.get_model("journal", "ExternalMediaIdentity")
    seen_entries = set()
    for identity in ExternalMediaIdentity.objects.order_by("entry_id", "id").iterator():
        if identity.entry_id in seen_entries:
            continue
        ExternalMediaIdentity.objects.filter(pk=identity.pk).update(is_metadata_source=True)
        seen_entries.add(identity.entry_id)


class Migration(migrations.Migration):
    dependencies = [
        ("plugin_host", "0001_initial"),
        ("journal", "0003_external_account_connections"),
    ]

    operations = [
        migrations.AlterField(
            model_name="journalentry",
            name="watch_status",
            field=models.CharField(
                choices=[
                    ("completed", "看过"),
                    ("watching", "在看"),
                    ("planned", "想看"),
                    ("on_hold", "搁置"),
                    ("dropped", "弃番"),
                ],
                default="planned",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="externalmediaidentity",
            name="is_metadata_source",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="externalmediaidentity",
            name="metadata_schema_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="externalimportsession",
            name="snapshot_schema_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.CreateModel(
            name="WatchHistoryRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("watched_on", models.DateField()),
                ("watched_label", models.CharField(max_length=80)),
                ("brush_number", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("brush_label", models.CharField(default="首刷", max_length=20)),
                ("episode_start", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("episode_end", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("notes", models.JSONField(blank=True, default=list)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("sequence", models.PositiveIntegerField()),
                ("semantic_key", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "entry",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="watch_history_records",
                        to="journal.journalentry",
                    ),
                ),
            ],
            options={"ordering": ["sequence", "id"]},
        ),
        migrations.RunPython(migrate_watch_history, migrations.RunPython.noop),
        migrations.RunPython(assign_metadata_sources, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="externalmediaidentity",
            constraint=models.UniqueConstraint(
                condition=Q(is_metadata_source=True),
                fields=("entry",),
                name="journal_ext_one_metadata_source_uq",
            ),
        ),
        migrations.AddConstraint(
            model_name="watchhistoryrecord",
            constraint=models.UniqueConstraint(
                fields=("entry", "semantic_key"),
                name="journal_watch_entry_semantic_uq",
            ),
        ),
        migrations.AddConstraint(
            model_name="watchhistoryrecord",
            constraint=models.UniqueConstraint(
                fields=("entry", "sequence"),
                name="journal_watch_entry_sequence_uq",
            ),
        ),
        migrations.AddConstraint(
            model_name="watchhistoryrecord",
            constraint=models.CheckConstraint(
                condition=Q(episode_start__isnull=True)
                | Q(episode_end__isnull=True)
                | Q(episode_end__gte=F("episode_start")),
                name="journal_watch_episode_range_ck",
            ),
        ),
        migrations.AddIndex(
            model_name="watchhistoryrecord",
            index=models.Index(
                fields=["entry", "watched_on"],
                name="journal_watch_entry_date_idx",
            ),
        ),
    ]
