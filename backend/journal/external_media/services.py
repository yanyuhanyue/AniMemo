from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from journal.domain_services import JournalEntryService
from journal.models import ExternalMediaIdentity, JournalEntry
from journal.poster_security import PosterUrlValidationError, validate_poster_url

from .errors import (
    external_identity_changed,
    identity_already_bound,
    identity_not_found,
    subject_already_bound,
)
from .registry import get_provider

SAFE_AUTO_FIELDS = ("japanese_title", "airing_period", "studio", "episodes", "poster_url")
USER_OWNED_FIELDS = (
    "title",
    "description",
    "tags",
    "personal_score",
    "watch_status",
    "review",
    "visibility",
    "custom_poster_url",
    "poster_file",
    "share_slug",
)


@dataclass(frozen=True)
class PreparedIdentity:
    provider: str
    external_id: str
    canonical_url: str
    metadata: dict
    metadata_fetched_at: object
    provider_updated_at: object = None


def prepare_identity(provider_slug, external_id, *, force=False):
    provider = get_provider(provider_slug)
    normalized_id = provider.normalize_external_id(external_id)
    metadata = provider.fetch_subject(normalized_id, force=force)
    return PreparedIdentity(
        provider=provider.slug,
        external_id=normalized_id,
        canonical_url=provider.canonical_url(normalized_id),
        metadata=metadata,
        metadata_fetched_at=timezone.now(),
    )


def lock_identity_owner(user):
    return get_user_model().objects.select_for_update().get(pk=user.pk)


def _duplicate_for_user(*, user, prepared, exclude_entry_id=None):
    queryset = ExternalMediaIdentity.objects.filter(
        entry__user=user,
        provider=prepared.provider,
        external_id=prepared.external_id,
    )
    if exclude_entry_id is not None:
        queryset = queryset.exclude(entry_id=exclude_entry_id)
    return queryset.select_related("entry").first()


def create_prepared_identity(entry, prepared):
    duplicate = _duplicate_for_user(user=entry.user, prepared=prepared, exclude_entry_id=entry.pk)
    if duplicate is not None:
        raise subject_already_bound(duplicate.entry_id)
    try:
        is_metadata_source = not ExternalMediaIdentity.objects.filter(
            entry=entry,
            is_metadata_source=True,
        ).exists()
        return ExternalMediaIdentity.objects.create(
            entry=entry,
            provider=prepared.provider,
            external_id=prepared.external_id,
            canonical_url=prepared.canonical_url,
            metadata=prepared.metadata,
            metadata_schema_version=1,
            is_metadata_source=is_metadata_source,
            metadata_fetched_at=prepared.metadata_fetched_at,
            provider_updated_at=prepared.provider_updated_at,
        )
    except IntegrityError as error:
        raise identity_already_bound() from error


def bind_external_identity(*, entry, user, provider_slug, external_id):
    provider = get_provider(provider_slug)
    if ExternalMediaIdentity.objects.filter(entry=entry, provider=provider.slug).exists():
        raise identity_already_bound()
    prepared = prepare_identity(provider_slug, external_id)
    return bind_prepared_external_identity(entry=entry, user=user, prepared=prepared)


def bind_prepared_external_identity(*, entry, user, prepared):
    with transaction.atomic():
        locked_user = lock_identity_owner(user)
        locked_entry = JournalEntry.objects.select_for_update().get(pk=entry.pk, user=locked_user, deleted_at__isnull=True)
        if ExternalMediaIdentity.objects.filter(entry=locked_entry, provider=prepared.provider).exists():
            raise identity_already_bound()
        duplicate = _duplicate_for_user(user=locked_user, prepared=prepared, exclude_entry_id=locked_entry.pk)
        if duplicate is not None:
            raise subject_already_bound(duplicate.entry_id)
        return create_prepared_identity(locked_entry, prepared)


def unbind_external_identity(*, entry, user, provider_slug):
    provider = get_provider(provider_slug)
    with transaction.atomic():
        locked_entry = JournalEntry.objects.select_for_update().get(pk=entry.pk, user=user, deleted_at__isnull=True)
        identity = ExternalMediaIdentity.objects.select_for_update().filter(entry=locked_entry, provider=provider.slug).first()
        if identity is None:
            raise identity_not_found()
        identity.delete()


def refresh_external_identity(*, entry, user, provider_slug):
    from journal.serializers_entries import JournalEntrySerializer

    provider = get_provider(provider_slug)
    current_identity = ExternalMediaIdentity.objects.filter(entry=entry, provider=provider.slug).first()
    if current_identity is None:
        raise identity_not_found()
    original_identity_pk = current_identity.pk
    original_external_id = current_identity.external_id
    metadata = provider.refresh(current_identity)
    fetched_at = timezone.now()

    with transaction.atomic():
        locked_entry = JournalEntry.objects.select_for_update().get(pk=entry.pk, user=user, deleted_at__isnull=True)
        identity = ExternalMediaIdentity.objects.select_for_update().filter(entry=locked_entry, provider=provider.slug).first()
        if (
            identity is None
            or identity.pk != original_identity_pk
            or identity.external_id != original_external_id
        ):
            raise external_identity_changed()

        provider_values = _entry_values(metadata)
        changed_fields = {}
        applied_fields = []
        update_values = {}
        if identity.is_metadata_source:
            for field in SAFE_AUTO_FIELDS:
                provider_value = provider_values.get(field)
                if provider_value in (None, ""):
                    continue
                current_value = getattr(locked_entry, field)
                if current_value == provider_value:
                    continue
                changed_fields[field] = {"current": current_value, "provider": provider_value}
                update_values[field] = provider_value
                applied_fields.append(field)

        if applied_fields:
            JournalEntryService(user).update_from_fields(
                locked_entry.pk,
                update_values,
                serializer_class=JournalEntrySerializer,
                source="external-media",
                allowed_fields=set(update_values),
            )
            locked_entry.refresh_from_db()
        identity.canonical_url = provider.canonical_url(identity.external_id)
        identity.metadata = metadata
        identity.metadata_schema_version = 1
        identity.metadata_fetched_at = fetched_at
        identity.provider_updated_at = None
        identity.save(
            update_fields=["canonical_url", "metadata", "metadata_schema_version", "metadata_fetched_at", "provider_updated_at", "updated_at"]
        )
    return identity, metadata, applied_fields, changed_fields


def set_metadata_source(*, entry, user, provider_slug, apply_metadata):
    from journal.serializers_entries import JournalEntrySerializer

    if not isinstance(apply_metadata, bool):
        raise ValueError("apply_metadata must be an explicit boolean")
    provider = get_provider(provider_slug)
    with transaction.atomic():
        locked_user = lock_identity_owner(user)
        locked_entry = JournalEntry.objects.select_for_update().get(
            pk=entry.pk,
            user=locked_user,
            deleted_at__isnull=True,
        )
        identities = list(
            ExternalMediaIdentity.objects.select_for_update()
            .filter(entry=locked_entry)
            .order_by("id")
        )
        selected = next((item for item in identities if item.provider == provider.slug), None)
        if selected is None:
            raise identity_not_found()
        ExternalMediaIdentity.objects.filter(entry=locked_entry, is_metadata_source=True).exclude(
            pk=selected.pk
        ).update(is_metadata_source=False, updated_at=timezone.now())
        if not selected.is_metadata_source:
            selected.is_metadata_source = True
            selected.save(update_fields=["is_metadata_source", "updated_at"])

        applied_fields = []
        changed_fields = {}
        if apply_metadata:
            provider_values = _entry_values(selected.metadata)
            update_values = {}
            for field in SAFE_AUTO_FIELDS:
                provider_value = provider_values.get(field)
                if provider_value in (None, ""):
                    continue
                current_value = getattr(locked_entry, field)
                if current_value == provider_value:
                    continue
                changed_fields[field] = {"current": current_value, "provider": provider_value}
                update_values[field] = provider_value
                applied_fields.append(field)
            if applied_fields:
                JournalEntryService(locked_user).update_from_fields(
                    locked_entry.pk,
                    update_values,
                    serializer_class=JournalEntrySerializer,
                    source="external-media",
                    allowed_fields=set(update_values),
                )
                locked_entry.refresh_from_db()
        return selected, applied_fields, changed_fields


def _entry_values(metadata):
    air_date = str(metadata.get("air_date") or "")[:32]
    parts = air_date.split("-")
    airing_period = ""
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        month = int(parts[1])
        if 1 <= month <= 12:
            airing_period = f"{parts[0]}-{month}"

    poster_url = ""
    if metadata.get("poster_url"):
        try:
            poster_url = validate_poster_url(metadata["poster_url"])
        except PosterUrlValidationError:
            poster_url = ""
        if len(poster_url) > _field_max_length("poster_url"):
            poster_url = ""
    episodes = metadata.get("episodes")
    return {
        "japanese_title": _entry_text(metadata.get("japanese_title"), "japanese_title"),
        "airing_period": airing_period,
        "studio": _entry_text(metadata.get("studio"), "studio"),
        "episodes": _entry_text(episodes, "episodes") if episodes not in (None, "", 0, "0") else "",
        "poster_url": poster_url,
    }


def _field_max_length(field_name):
    return JournalEntry._meta.get_field(field_name).max_length


def _entry_text(value, field_name):
    return str(value or "")[:_field_max_length(field_name)]
