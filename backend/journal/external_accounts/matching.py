from journal.models import ExternalMediaIdentity, JournalEntry


def conflicts(entry, row):
    result = {}
    pairs = (
        ("personal_score", None if entry.personal_score is None else float(entry.personal_score), row.get("remote_rating")),
        ("watch_status", entry.watch_status, row.get("remote_status")),
        ("review", entry.review or "", row.get("remote_comment") or ""),
    )
    for field, local, remote in pairs:
        if remote in (None, "") or local in (None, "") or local == remote:
            continue
        result[field] = {"local": local, "remote": remote}
    return result


def annotate_matches(user, provider_slug, rows):
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
                "conflicts": conflicts(entry, row),
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


def bind_targets(user, provider_slug):
    bound_ids = ExternalMediaIdentity.objects.filter(
        entry__user=user,
        entry__deleted_at__isnull=True,
        provider=provider_slug,
    ).values_list("entry_id", flat=True)
    return [
        {"id": entry.pk, "title": entry.title}
        for entry in JournalEntry.objects.filter(
            user=user,
            deleted_at__isnull=True,
        ).exclude(pk__in=bound_ids).order_by("title", "id")[:500]
    ]
