import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal

from config.credentials import CredentialCipher
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from journal.external_media.errors import ExternalMediaError
from journal.external_media.services import (
    bind_prepared_external_identity,
    create_prepared_identity,
    lock_identity_owner,
    prepare_identity,
)
from journal.models import (
    ExternalAccountAuthorizationState,
    ExternalImportSession,
    ExternalMediaIdentity,
    JournalEntry,
    UserExternalAccountConnection,
)

from .credentials import (
    ExternalAccountCredentialError,
    decrypt_credentials,
    encrypt_credentials,
)
from .errors import (
    ExternalAccountError,
    account_already_connected,
    account_identity_mismatch,
    account_not_configured,
    account_not_connected,
    account_token_invalid,
    authorization_state_expired,
    authorization_state_invalid,
    import_item_invalid,
    import_preview_expired,
)
from .registry import get_account_provider

IMPORT_MODES = {"CREATE_NEW", "BIND_EXISTING", "IMPORT_SAFE_USER_FIELDS", "SKIP"}
IMPORTABLE_USER_FIELDS = {"personal_score", "watch_status", "review"}


def account_integration_enabled():
    return bool(getattr(settings, "BANGUMI_ACCOUNT_INTEGRATION_ENABLED", True))


def provider_capability(provider_slug):
    provider = get_account_provider(provider_slug)
    enabled = account_integration_enabled()
    return {
        "provider": provider.slug,
        "display_name": provider.display_name,
        "enabled": enabled,
        "oauth_available": enabled and provider.oauth_available(),
        "personal_access_token_available": enabled,
    }


def serialize_connection(connection):
    if connection is None:
        return None
    return {
        "provider": connection.provider,
        "connected": True,
        "auth_method": str(connection.auth_method),
        "external_user_id": connection.external_user_id,
        "username": connection.external_username,
        "display_name": connection.display_name,
        "avatar_url": str(connection.metadata.get("avatar_url") or "") if isinstance(connection.metadata, dict) else "",
        "status": str(connection.status),
        "connected_at": connection.connected_at,
        "verified_at": connection.verified_at,
        "last_used_at": connection.last_used_at,
        "expires_at": connection.expires_at,
    }


def list_account_providers(user):
    connection = UserExternalAccountConnection.objects.filter(user=user, provider="bangumi").first()
    capability = provider_capability("bangumi")
    capability["connection"] = serialize_connection(connection)
    return [capability]


def _ensure_enabled():
    if not account_integration_enabled():
        raise account_not_configured()


def _expires_at(expires_in):
    if not expires_in:
        return None
    return timezone.now() + timedelta(seconds=int(expires_in))


def connect_account(*, user, provider_slug, credentials, auth_method, expires_in=None):
    _ensure_enabled()
    provider = get_account_provider(provider_slug)
    access_token = str(credentials.get("access_token") or "").strip()
    profile = provider.verify_account(access_token)
    ciphertext = encrypt_credentials(credentials)
    now = timezone.now()
    try:
        with transaction.atomic():
            locked_user = lock_identity_owner(user)
            connection = UserExternalAccountConnection.objects.select_for_update().filter(
                user=locked_user,
                provider=provider.slug,
            ).first()
            if connection is not None and connection.external_user_id != profile["external_user_id"]:
                raise account_identity_mismatch()
            if connection is None:
                connection = UserExternalAccountConnection(
                    user=locked_user,
                    provider=provider.slug,
                    external_user_id=profile["external_user_id"],
                    connected_at=now,
                )
            connection.auth_method = auth_method
            connection.external_username = profile["external_username"]
            connection.display_name = profile["display_name"]
            connection.credential_ciphertext = ciphertext
            connection.credential_key_version = CredentialCipher.version
            connection.metadata = profile["metadata"]
            connection.status = UserExternalAccountConnection.Status.CONNECTED
            connection.verified_at = now
            connection.last_used_at = now
            connection.expires_at = _expires_at(expires_in)
            connection.save()
            return connection
    except IntegrityError as error:
        raise account_already_connected() from error


def connect_personal_access_token(*, user, provider_slug, access_token):
    token = str(access_token or "").strip()
    return connect_account(
        user=user,
        provider_slug=provider_slug,
        credentials={"access_token": token, "token_type": "Bearer"},
        auth_method=UserExternalAccountConnection.AuthMethod.PERSONAL_ACCESS_TOKEN,
    )


def get_connection(*, user, provider_slug, for_update=False):
    provider = get_account_provider(provider_slug)
    queryset = UserExternalAccountConnection.objects
    if for_update:
        queryset = queryset.select_for_update()
    connection = queryset.filter(user=user, provider=provider.slug).first()
    if connection is None:
        raise account_not_connected()
    return connection


def _mark_needs_reauthorization(connection):
    UserExternalAccountConnection.objects.filter(pk=connection.pk).update(
        status=UserExternalAccountConnection.Status.NEEDS_REAUTHORIZATION,
        updated_at=timezone.now(),
    )


def _access_token(connection, provider):
    try:
        credentials = decrypt_credentials(connection.credential_ciphertext)
    except ExternalAccountCredentialError as error:
        _mark_needs_reauthorization(connection)
        raise account_token_invalid() from error
    refresh_token = credentials.get("refresh_token")
    should_refresh = (
        connection.auth_method == UserExternalAccountConnection.AuthMethod.OAUTH
        and connection.expires_at is not None
        and connection.expires_at <= timezone.now() + timedelta(seconds=30)
    )
    if not should_refresh:
        return credentials["access_token"]
    if not refresh_token or not provider.oauth_available():
        _mark_needs_reauthorization(connection)
        raise account_token_invalid()
    try:
        refreshed = provider.refresh_oauth_token(refresh_token)
        profile = provider.verify_account(refreshed["access_token"])
    except ExternalAccountError as error:
        _mark_needs_reauthorization(connection)
        raise error
    if profile["external_user_id"] != connection.external_user_id:
        _mark_needs_reauthorization(connection)
        raise account_identity_mismatch()
    encrypted = encrypt_credentials(refreshed)
    with transaction.atomic():
        current = UserExternalAccountConnection.objects.select_for_update().get(pk=connection.pk)
        if current.external_user_id != profile["external_user_id"]:
            raise account_identity_mismatch()
        current.credential_ciphertext = encrypted
        current.credential_key_version = CredentialCipher.version
        current.expires_at = _expires_at(refreshed["expires_in"])
        current.external_username = profile["external_username"]
        current.display_name = profile["display_name"]
        current.metadata = profile["metadata"]
        current.status = UserExternalAccountConnection.Status.CONNECTED
        current.verified_at = timezone.now()
        current.last_used_at = current.verified_at
        current.save()
        connection.expires_at = current.expires_at
    return refreshed["access_token"]


def verify_connection(*, user, provider_slug):
    _ensure_enabled()
    connection = get_connection(user=user, provider_slug=provider_slug)
    provider = get_account_provider(provider_slug)
    token = _access_token(connection, provider)
    try:
        profile = provider.verify_account(token)
    except ExternalAccountError as error:
        if error.detail.get("code") == "external_account_token_invalid":
            _mark_needs_reauthorization(connection)
        raise
    if profile["external_user_id"] != connection.external_user_id:
        _mark_needs_reauthorization(connection)
        raise account_identity_mismatch()
    now = timezone.now()
    UserExternalAccountConnection.objects.filter(pk=connection.pk).update(
        external_username=profile["external_username"],
        display_name=profile["display_name"],
        metadata=profile["metadata"],
        status=UserExternalAccountConnection.Status.CONNECTED,
        verified_at=now,
        last_used_at=now,
        updated_at=now,
    )
    connection.refresh_from_db()
    return connection


def disconnect_account(*, user, provider_slug):
    with transaction.atomic():
        connection = get_connection(user=user, provider_slug=provider_slug, for_update=True)
        connection.delete()


def _state_digest(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def start_oauth_authorization(*, user, provider_slug):
    _ensure_enabled()
    provider = get_account_provider(provider_slug)
    if not provider.oauth_available():
        raise account_not_configured()
    now = timezone.now()
    ExternalAccountAuthorizationState.objects.filter(expires_at__lt=now).delete()
    raw_state = secrets.token_urlsafe(32)
    ExternalAccountAuthorizationState.objects.create(
        user=user,
        provider=provider.slug,
        state_digest=_state_digest(raw_state),
        expires_at=now + timedelta(seconds=int(settings.BANGUMI_OAUTH_STATE_TTL_SECONDS)),
    )
    return provider.authorization_url(raw_state)


def _consume_authorization_state(provider_slug, raw_state):
    digest = _state_digest(raw_state)
    now = timezone.now()
    with transaction.atomic():
        state = ExternalAccountAuthorizationState.objects.select_for_update().select_related("user").filter(
            provider=provider_slug,
            state_digest=digest,
        ).first()
        if state is None or state.consumed_at is not None:
            raise authorization_state_invalid()
        if state.expires_at <= now:
            raise authorization_state_expired()
        state.consumed_at = now
        state.save(update_fields=["consumed_at"])
        return state.user


def complete_oauth_authorization(*, provider_slug, code, state):
    _ensure_enabled()
    provider = get_account_provider(provider_slug)
    if not provider.oauth_available():
        raise account_not_configured()
    code = str(code or "").strip()
    state = str(state or "").strip()
    if not code or len(code) > 512 or not state or len(state) > 512:
        raise authorization_state_invalid()
    user = _consume_authorization_state(provider.slug, state)
    credentials = provider.exchange_code(code, state)
    return connect_account(
        user=user,
        provider_slug=provider.slug,
        credentials=credentials,
        auth_method=UserExternalAccountConnection.AuthMethod.OAUTH,
        expires_in=credentials["expires_in"],
    )


def _connection_token_for_use(*, user, provider_slug):
    connection = get_connection(user=user, provider_slug=provider_slug)
    provider = get_account_provider(provider_slug)
    return connection, provider, _access_token(connection, provider)


def _conflicts(entry, row):
    conflicts = {}
    pairs = (
        ("personal_score", None if entry.personal_score is None else float(entry.personal_score), row.get("remote_rating")),
        ("watch_status", entry.watch_status, row.get("remote_status")),
        ("review", entry.review or "", row.get("remote_comment") or ""),
    )
    for field, local, remote in pairs:
        if remote in (None, "") or local in (None, "") or local == remote:
            continue
        conflicts[field] = {"local": local, "remote": remote}
    return conflicts


def _annotate_matches(user, provider_slug, rows):
    identities = ExternalMediaIdentity.objects.filter(
        entry__user=user,
        entry__deleted_at__isnull=True,
        provider=provider_slug,
    ).select_related("entry")
    by_external_id = {identity.external_id: identity.entry for identity in identities}
    bound_entry_ids = {entry.pk for entry in by_external_id.values()}
    entries = list(JournalEntry.objects.filter(user=user, deleted_at__isnull=True).order_by("title", "id"))
    unbound_entries = [entry for entry in entries if entry.pk not in bound_entry_ids]
    title_index = {}
    for entry in unbound_entries:
        for title in (entry.title, entry.japanese_title):
            normalized = str(title or "").strip().casefold()
            if normalized:
                title_index.setdefault(normalized, []).append(entry)
    annotated = []
    for source in rows:
        row = dict(source)
        entry = by_external_id.get(row["external_id"])
        if entry is not None:
            row.update({
                "match_state": "already_bound",
                "local_entry_id": entry.pk,
                "local_entry_title": entry.title,
                "conflicts": _conflicts(entry, row),
                "possible_local_matches": [],
            })
        else:
            candidates = []
            candidate_ids = set()
            for title in (row.get("title"), row.get("japanese_title")):
                for candidate in title_index.get(str(title or "").strip().casefold(), []):
                    if candidate.pk not in candidate_ids:
                        candidate_ids.add(candidate.pk)
                        candidates.append(candidate)
            row.update({
                "match_state": "possible_local_match" if candidates else "unbound",
                "local_entry_id": None,
                "local_entry_title": "",
                "conflicts": {},
                "possible_local_matches": [
                    {"id": candidate.pk, "title": candidate.title}
                    for candidate in candidates[:5]
                ],
            })
        annotated.append(row)
    return annotated


def _bind_targets(user, provider_slug):
    bound_ids = ExternalMediaIdentity.objects.filter(
        entry__user=user,
        entry__deleted_at__isnull=True,
        provider=provider_slug,
    ).values_list("entry_id", flat=True)
    return [
        {"id": entry.pk, "title": entry.title}
        for entry in JournalEntry.objects.filter(user=user, deleted_at__isnull=True).exclude(pk__in=bound_ids).order_by("title", "id")[:500]
    ]


def create_import_preview(*, user, provider_slug):
    _ensure_enabled()
    connection, provider, token = _connection_token_for_use(user=user, provider_slug=provider_slug)
    try:
        remote_rows = provider.get_collections(
            token,
            connection.external_username,
            max_items=int(settings.BANGUMI_IMPORT_MAX_ITEMS),
        )
    except ExternalAccountError as error:
        if error.detail.get("code") == "external_account_token_invalid":
            _mark_needs_reauthorization(connection)
        raise
    now = timezone.now()
    rows = _annotate_matches(user, provider.slug, remote_rows)
    ExternalImportSession.objects.filter(expires_at__lt=now).delete()
    session = ExternalImportSession.objects.create(
        user=user,
        provider=provider.slug,
        snapshot=rows,
        expires_at=now + timedelta(seconds=int(settings.BANGUMI_IMPORT_PREVIEW_TTL_SECONDS)),
    )
    UserExternalAccountConnection.objects.filter(pk=connection.pk).update(last_used_at=now, updated_at=now)
    return session


def get_import_preview(*, user, provider_slug, preview_id, page=1, page_size=24, filter_value="all", query=""):
    provider = get_account_provider(provider_slug)
    session = ExternalImportSession.objects.filter(pk=preview_id, user=user, provider=provider.slug).first()
    if session is None or session.expires_at <= timezone.now() or session.applied_at is not None:
        raise import_preview_expired()
    rows = list(session.snapshot) if isinstance(session.snapshot, list) else []
    filter_value = str(filter_value or "all").strip().lower()
    if filter_value in {"planned", "watching", "completed", "on_hold"}:
        rows = [row for row in rows if row.get("remote_status") == filter_value]
    elif filter_value == "conflict":
        rows = [row for row in rows if row.get("conflicts") or row.get("match_state") == "possible_local_match"]
    elif filter_value == "existing":
        rows = [row for row in rows if row.get("match_state") == "already_bound"]
    query = str(query or "").strip().casefold()[:100]
    if query:
        rows = [row for row in rows if query in str(row.get("title") or "").casefold() or query in str(row.get("japanese_title") or "").casefold()]
    try:
        page_size = min(50, max(1, int(page_size or 24)))
        page = max(1, int(page or 1))
    except (TypeError, ValueError) as error:
        raise import_item_invalid("分页参数无效。") from error
    total = len(rows)
    start = (page - 1) * page_size
    summary = {
        "remote_count": len(session.snapshot) if isinstance(session.snapshot, list) else 0,
        "already_bound": sum(1 for row in session.snapshot if row.get("match_state") == "already_bound"),
        "possible_duplicates": sum(1 for row in session.snapshot if row.get("match_state") == "possible_local_match"),
        "conflicts": sum(1 for row in session.snapshot if row.get("conflicts")),
    }
    return {
        "preview_id": str(session.pk),
        "expires_at": session.expires_at,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
        "results": rows[start:start + page_size],
        "summary": summary,
        "bind_targets": _bind_targets(user, provider.slug),
    }


def _airing_period(metadata):
    parts = str(metadata.get("air_date") or "")[:32].split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and 1 <= int(parts[1]) <= 12:
        return f"{parts[0]}-{int(parts[1])}"
    return ""


def _create_imported_entry(user, prepared, row):
    metadata = prepared.metadata
    rating = row.get("remote_rating")
    entry = JournalEntry.objects.create(
        user=user,
        title=str(metadata.get("title") or row.get("title") or f"Bangumi #{prepared.external_id}")[:200],
        japanese_title=str(metadata.get("japanese_title") or "")[:200],
        airing_period=_airing_period(metadata)[:50],
        studio=str(metadata.get("studio") or "")[:120],
        episodes=str(metadata.get("episodes") or "")[:30],
        description=str(metadata.get("summary") or "")[:10000],
        poster_url=str(metadata.get("poster_url") or "")[:1000],
        tags=[str(tag)[:100] for tag in (metadata.get("tags") or [])[:30]],
        personal_score=Decimal(str(rating)) if rating is not None else None,
        watch_status=row.get("remote_status") or JournalEntry.WatchStatus.PLANNED,
        review=str(row.get("remote_comment") or "")[:10000],
    )
    identity = create_prepared_identity(entry, prepared)
    _record_import_provenance(identity, row)
    return entry


def _record_import_provenance(identity, row):
    metadata = dict(identity.metadata) if isinstance(identity.metadata, dict) else {}
    metadata["import_provenance"] = {
        "source": identity.provider,
        "imported_at": timezone.now().isoformat(),
        "collection_status": row.get("remote_status"),
        "collection_updated_at": row.get("remote_updated_at") or "",
    }
    identity.metadata = metadata
    identity.save(update_fields=["metadata", "updated_at"])


def _apply_explicit_user_fields(entry, row, fields):
    changed = []
    if "personal_score" in fields and row.get("remote_rating") is not None:
        entry.personal_score = Decimal(str(row["remote_rating"]))
        changed.append("personal_score")
    if "watch_status" in fields and row.get("remote_status") in JournalEntry.WatchStatus.values:
        entry.watch_status = row["remote_status"]
        changed.append("watch_status")
    if "review" in fields and row.get("remote_comment"):
        entry.review = str(row["remote_comment"])[:10000]
        changed.append("review")
    if changed:
        entry.save(update_fields=[*changed, "updated_at"])
    return changed


def _apply_import_item(*, user, provider_slug, row, action, prepared):
    mode = action["mode"]
    fields = action["apply_fields"]
    if mode == "SKIP":
        return {"external_id": row["external_id"], "status": "skipped"}
    if mode == "CREATE_NEW":
        if prepared is None:
            raise import_item_invalid("无法读取该作品的权威资料。")
        entry = _create_imported_entry(user, prepared, row)
        return {"external_id": row["external_id"], "status": "created", "entry_id": entry.pk}
    if mode == "BIND_EXISTING":
        if prepared is None:
            raise import_item_invalid("无法读取该作品的权威资料。")
        entry_id = action.get("local_entry_id")
        entry = JournalEntry.objects.filter(pk=entry_id, user=user, deleted_at__isnull=True).first()
        if entry is None:
            raise import_item_invalid("要绑定的本地记录不存在。")
        identity = bind_prepared_external_identity(entry=entry, user=user, prepared=prepared)
        _record_import_provenance(identity, row)
        changed = _apply_explicit_user_fields(entry, row, fields)
        return {"external_id": row["external_id"], "status": "bound", "entry_id": entry.pk, "updated_fields": changed}
    identity = ExternalMediaIdentity.objects.select_related("entry").filter(
        entry__user=user,
        entry__deleted_at__isnull=True,
        provider=provider_slug,
        external_id=row["external_id"],
    ).first()
    if identity is None:
        raise import_item_invalid("该作品当前未绑定，无法导入用户字段。")
    changed = _apply_explicit_user_fields(identity.entry, row, fields)
    return {
        "external_id": row["external_id"],
        "status": "updated" if changed else "skipped",
        "entry_id": identity.entry_id,
        "updated_fields": changed,
    }


def _normalize_actions(items):
    if not isinstance(items, list) or not items:
        raise import_item_invalid("请选择至少一个导入项目。")
    if len(items) > int(settings.BANGUMI_IMPORT_APPLY_MAX_ITEMS):
        raise import_item_invalid("单次导入项目过多，请分批处理。")
    actions = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            actions.append({
                "external_id": "",
                "mode": "SKIP",
                "local_entry_id": None,
                "apply_fields": set(),
                "error": "import_item_invalid",
            })
            continue
        external_id = str(item.get("external_id") or "").strip()
        mode = str(item.get("mode") or "SKIP").strip().upper()
        fields = item.get("apply_fields") or []
        local_entry_id = item.get("local_entry_id")
        error = None
        if not external_id or len(external_id) > 200 or external_id in seen or mode not in IMPORT_MODES:
            error = "import_item_invalid"
        elif not isinstance(fields, list) or any(field not in IMPORTABLE_USER_FIELDS for field in fields):
            error = "import_item_invalid"
        elif mode == "BIND_EXISTING":
            try:
                local_entry_id = int(local_entry_id)
                if local_entry_id < 1:
                    raise ValueError
            except (TypeError, ValueError):
                error = "import_item_invalid"
                local_entry_id = None
        else:
            local_entry_id = None
        if external_id:
            seen.add(external_id)
        actions.append({
            "external_id": external_id,
            "mode": mode,
            "local_entry_id": local_entry_id,
            "apply_fields": set(fields) if isinstance(fields, list) else set(),
            "error": error,
        })
    return actions


def apply_import_preview(*, user, provider_slug, preview_id, items):
    provider = get_account_provider(provider_slug)
    actions = _normalize_actions(items)
    initial = ExternalImportSession.objects.filter(pk=preview_id, user=user, provider=provider.slug).first()
    if initial is None or initial.expires_at <= timezone.now():
        raise import_preview_expired()
    if initial.applied_at is not None:
        return initial.result
    rows_by_id = {
        str(row.get("external_id")): row
        for row in initial.snapshot
        if isinstance(row, dict) and row.get("external_id")
    }
    prepared = {}
    preparation_errors = {}
    for action in actions:
        if action["error"]:
            continue
        row = rows_by_id.get(action["external_id"])
        if row is None:
            preparation_errors[action["external_id"]] = "import_item_invalid"
            continue
        if action["mode"] not in {"CREATE_NEW", "BIND_EXISTING"}:
            continue
        try:
            prepared[action["external_id"]] = prepare_identity(provider.slug, action["external_id"])
        except ExternalMediaError as error:
            preparation_errors[action["external_id"]] = str(error.detail.get("code") or "provider_unavailable")

    with transaction.atomic():
        session = ExternalImportSession.objects.select_for_update().filter(
            pk=preview_id,
            user=user,
            provider=provider.slug,
        ).first()
        if session is None or session.expires_at <= timezone.now():
            raise import_preview_expired()
        if session.applied_at is not None:
            return session.result
        lock_identity_owner(user)
        results = []
        for action in actions:
            external_id = action["external_id"]
            row = rows_by_id.get(external_id)
            if action["error"] or row is None or external_id in preparation_errors:
                results.append({
                    "external_id": external_id,
                    "status": "failed",
                    "code": action["error"] or preparation_errors.get(external_id, "import_item_invalid"),
                })
                continue
            try:
                with transaction.atomic():
                    results.append(_apply_import_item(
                        user=user,
                        provider_slug=provider.slug,
                        row=row,
                        action=action,
                        prepared=prepared.get(external_id),
                    ))
            except (ExternalAccountError, ExternalMediaError, IntegrityError) as error:
                detail = getattr(error, "detail", {})
                results.append({
                    "external_id": external_id,
                    "status": "conflict" if getattr(error, "status_code", None) == 409 else "failed",
                    "code": str(detail.get("code") or "import_conflict"),
                })
        counts = {name: sum(1 for item in results if item["status"] == name) for name in ("created", "bound", "updated", "skipped", "conflict", "failed")}
        result = {"preview_id": str(session.pk), "results": results, "counts": counts}
        session.snapshot = []
        session.result = result
        session.applied_at = timezone.now()
        session.save(update_fields=["snapshot", "result", "applied_at", "updated_at"])
        return result
