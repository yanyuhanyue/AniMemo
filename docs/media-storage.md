# Media Storage Pool

Production media uses `site_config.media_storage.storage.StoragePoolStorage`. The database-backed pool supports `cloudflare_r2` and `local` backends. New writes use the preferred backend while it remains writable; otherwise enabled backends are tried in ascending `priority` order. A successful failover becomes the preferred backend, so a recovered backend is not automatically selected again.

Every production upload creates a `site_config.models.MediaObject` row. Existing `ImageField` values contain a stable `media-objects/<uuid>` reference, and the row records the original backend plus object key. Reads, URLs, deletes, and replacements resolve through that row rather than guessing from the current preferred backend.

JournalEntry media fields, authoritative holdings and logical quota changes commit or roll back together. Entry serializers, domain services, HTTP projection and Admin transactions use `atomic_media_mutation()` so an outer failure can reclaim only that mutation's uncommitted uploads. Replacing or removing a poster releases that business slot after commit; the old object's bytes are reclaimed only when the lifecycle check proves that no valid reference or write protection remains. A callback failure after commit preserves a new upload whose `MediaObject` has committed.

New physical uploads use the existing `MediaWriteReservation`. On PostgreSQL the reservation commits through an independent database connection before adapter I/O; finalization creates the `MediaObject` and marks the receipt `finalized` in the business transaction. `pending`, `cleanup_ready` and `cleanup_failed` receipts continue to occupy physical capacity. Expiration alone never proves that a writer stopped and never releases this capacity. A completed or rolled-back caller can mark its exact upload for cleanup. Successful cleanup changes its receipt to `abandoned`; failure preserves its identity, bytes and error class for explicit retry. SQLite development mode compensates after its outer writer unwinds and is not the PostgreSQL concurrency guarantee.

DEBUG uses `DevelopmentFileStorage`, an exclusive-write filesystem backend. It records the actual file identity before writing and can remove only that mutation's uncommitted file after a failed write or outer rollback. Existing development files retain their filesystem names and count their actual size toward poster quota. This development compensation does not provide PostgreSQL's durable receipt or cross-process recovery guarantee.

R2 credentials are encrypted with `config.credentials.CredentialCipher` using the independent `CREDENTIAL_ENCRYPTION_KEY`. The API exposes only `*_configured` flags. R2 client caches are keyed by `config_version`, so a backend edit is picked up by every worker on its next operation without a restart.

Cloudflare usage snapshots are observability data. Managed physical usage is the sum of `MediaObject.size_bytes` and occupying write receipts (`pending`, `cleanup_ready`, `cleanup_failed`), read in one SQL snapshot so the reservation-to-object handoff is not omitted. Finalized receipts do not add a second physical charge. The R2 write guard conservatively uses `max(managed_usage, last_known_cloudflare_snapshot)`. Local backends also check real disk availability and include the entire incoming upload before writing. Replacing a poster cannot borrow capacity from an old object that might be deleted later; adding a URL holding performs no physical upload or second reservation.

R2 thresholds use decimal GB (`1 GB = 1,000,000,000 bytes`). Local disk reserves use GiB (`1 GiB = 1,073,741,824 bytes`) and are labeled as GiB in the admin UI. Optional `CloudflareR2Account` budgets aggregate managed bytes and refreshed bucket snapshots across all linked buckets, so two buckets in one account share one configured account limit; accounts without a configured limit do not add a guard.

For periodic external usage refresh, run the built-in command from a host scheduler every 30–60 minutes as appropriate for the deployment. On a canonical v1.1 host, invoke it through the API container with the fixed project, exact Compose materials, and the runtime Adapter derived from managed configuration:

```bash
cd /opt/animemo
docker compose --project-name animemo \
  --env-file /run/animemo-updater/managed.env \
  -f /opt/animemo/deploy/docker-compose.yml \
  -f /opt/animemo/updater/docker-compose.runtime.yml \
  exec -T api python manage.py refresh_media_storage_usage
```

The command reports only backend slugs and `success/failed/skipped` counts. It never prints access keys, secrets, analytics tokens, or authorization headers. One failing R2 refresh does not prevent the remaining backends from being attempted.

The media storage admin API and UI are Superuser-only:

- `GET/POST /api/v1/staff/system/media-storage/`
- `GET/PATCH/DELETE /api/v1/staff/system/media-storage/<id>/`
- `POST /api/v1/staff/system/media-storage/<id>/actions/`

Use `action=test-connection`, `refresh-usage`, `set-active`, `toggle-writes`, or `clear-credentials` for explicit operations. Production without any configured backend starts normally and returns `MEDIA_STORAGE_SETUP_REQUIRED` for new media writes; it never silently falls back to a local directory.

## Managed poster URLs and ownership

A successful `custom_poster_url` write establishes a durable holding when the URL uniquely identifies an existing managed object and the authenticated or explicitly supplied service owner has provable ownership. The accepted business URL is retained after the existing input validation; it is not replaced with a storage key or internal UUID. Third-party, cross-owner and unidentifiable links remain link-only, receive no new managed holding or poster charge, and gain no new access rights.

Identity comes from the configured backend public base and exact object key, approved server history bases keyed by stable backend database ID, or the server-created `public_url_snapshot` and matching identity digest. For LOCAL backends the public base includes `local_root`. The snapshot preserves an upload's original origin across later public-base edits; it is identity evidence, not ownership evidence. Request `Host`, `X-Forwarded-Host`, body fields and the poster host allowlist cannot establish an instance mapping. The resolver performs no URL GET/HEAD, redirect following or remote download.

The identity parser compares HTTPS scheme, lowercase host, port and case-sensitive path. An omitted port and `:443` identify the same default HTTPS origin; port zero and userinfo do not identify managed objects. Query and fragment do not participate in object identity and remain in the stored business URL. Paths are decoded once with strict UTF-8; encoded separators, encoded percent signs, malformed percent escapes, backslashes, repeated slashes, dot segments and control characters cannot identify a managed object. The parser does not evaluate signatures or expiry parameters, so retaining bytes does not make a temporary URL permanently usable. New API input still requires an absolute HTTPS URL accepted by the poster safety policy. Historical single-leading-slash paths can be inspected or backfilled only through the configured `ANIMEMO_MEDIA_PUBLIC_ORIGIN`.

`MEDIA_HISTORICAL_PUBLIC_BASES` is a server Django setting shaped as `{backend_id_string: [approved_base_url, ...]}`. It is not a user field, a poster allowlist entry or an environment-variable shortcut supported by the default settings module. Historical mappings must be supplied and verified through the deployment's server settings before backfill.

Ownership requires one consistent owner across persisted evidence: `upload_owner`, direct entry poster associations, user avatar associations, column author/cover associations and existing journal holdings. A URL, path segment, filename, public readability or matching SHA cannot supply an owner. Conflicting or missing evidence is not repaired by guessing. `upload_owner` records provenance; it does not itself keep an otherwise unreferenced object alive.

`JournalMediaReference` has one row per `(entry, slot)`, where the slot is `poster_file` or `custom_poster_url`; its `MediaObject` foreign key uses `PROTECT`. Soft-deleted entries retain their holdings. Existing nonposter roles protect bytes without adding poster quota:

| Persisted association | Ownership evidence | Prevents cleanup | Poster logical charge |
| --- | --- | --- | --- |
| Entry `poster_file` | Entry owner | Yes | One populated slot |
| Managed `custom_poster_url` holding | Proven entry owner | Yes | One populated slot |
| `UserSettings.avatar` | User owner | Yes | No |
| `Column.cover` | Column author | Yes | No |
| `SiteSettings.site_avatar` | No personal owner is inferred | Yes | No |

New managed URL holdings validate actual LOCAL bytes against the recorded positive size and SHA-256 while holding the object lifecycle lock. Missing, invalid, unknown-sized or deleting objects cannot yield a successful new managed holding. R2 lacks the complete-byte proof required for new URL admission. Existing direct-field associations remain protected and can pass reference inventory when their metadata and relationships are complete; `objects[].byte_verification` still reports `UNVERIFIED_REMOTE_BYTES`. That status does not alone block a direct association, but it cannot establish a historical or new R2 URL holding. Such URL claims remain blocked. READY proves reference inventory, not remote-byte health or complete R2 Restore coverage.

## Logical poster quota and cleanup

Poster quota is a byte allowance, not a monetary charge. `POSTER_STORAGE_QUOTA_BYTES` remains the configured limit; its code default is 524,288,000 bytes. Each populated poster business slot counts once using the trustworthy stored object's size, including soft-deleted entries. Two entries holding one object consume two logical charges and one physical object. Both stored slots count if independently populated; display priority does not remove a saved reference. Ordinary REST poster selection/clearing behavior remains unchanged. Provider metadata `poster_url`, link-only URLs, avatars and column/site images do not acquire this new poster charge.

Repeated values, unrelated PATCHes and restore-from-trash do not create duplicate holdings. Media mutations serialize per owner and reread usage inside the transaction, then use the net change in affected slots. Known usage at or below quota may increase only within quota; historical over-quota usage may remain equal or decrease but may not increase. Unknown sizes remain `UNKNOWN_USAGE`: increases depending on unknown accounting fail, while provably safe reads, unrelated changes and releases remain possible. Backfill does not delete old data or raise quota to hide historical overage.

The media lock order is owner, entries in primary-key order, media objects in UUID order, then physical pool/backend admission. Cleanup takes no owner lock. It locks the object, verifies the inventory gate and all direct/held/nonposter/write protections, and commits `deleting` before physical deletion. Adapter deletion is serialized under a second object lock with another protection check. New holdings cannot attach to `deleting`. Failed deletion retains the object and physical accounting as `delete_failed`; a valid new holding can reactivate it only after complete-byte validation. Finalized physical receipts remain to prevent reuse of the same object key after deletion.

## Historical backfill and explicit recovery

Deploy the additive schema and compatible writers before historical backfill. Legacy objects start with `reference_inventory_complete=false`; unresolved objects cannot be reclaimed. Writers that ignore the holder schema must stop before the v2 migration. Updater and Restore bootstrap run the complete backfill and require READY before starting the target API.

Use the same managed Compose/API-container invocation shown in this document for these management commands:

~~~text
python manage.py reconcile_media_references
python manage.py reconcile_media_references --apply --limit 200
python manage.py reconcile_media_references --apply --all-batches --limit 200
~~~

The first is read-only. `--apply` commits per-entry backfill while preserving business URLs and bytes. Resume a partial batch with the actual returned `next_after_entry` integer passed as `--after-entry`; `--limit` accepts 1–1000. `--all-batches` continues the invocation's remaining entry snapshot and still requires a final full inventory. READY exits successfully; INCOMPLETE or BLOCKED prints the report and exits nonzero. A nonzero apply may already have committed valid backfill work, so preserve its report and resume or repeat the idempotent command.

Inspect `objects`, `slots`, `other_roles`, `unresolved_writes` and `usage`. Missing stable rows, invalid/missing bytes, unknown size, unproven/conflicting owner, ambiguous URLs and mismatched/orphaned holdings remain diagnostics. Existing conflicting rows are not silently overwritten. A URL that cannot identify any extant object remains link-only; the command cannot reconstruct a deleted object's identity, owner or bytes from its URL.

After resolving the reported cause, explicit recovery uses:

~~~text
python manage.py reconcile_media_references --retry-cleanup
python manage.py reconcile_media_write_reservations
python manage.py reconcile_media_write_reservations --retry-cleanup
~~~

The first retries only objects in `deleting` or `delete_failed` and rechecks current protection; it does not sweep every active orphan. The second reports unresolved expired writes without changing them. The third retries only receipts already marked `cleanup_ready` or `cleanup_failed`, for their exact backend/key. Unresolved `pending` writers remain protected regardless of age. Retry can delete storage bytes and is distinct from default read-only inventory. Neither command recreates missing media. Actual instance migration/recovery requires the deployment's authorized maintenance operation and verified backup; isolated tests do not establish that an instance has been migrated.

## Remote orphan runbook

Default maintenance reports unresolved physical writes and may audit database state; it does not enumerate R2 objects or abandon a pending writer merely because it is old. Explicit receipt cleanup can delete only the exact known upload already proven eligible for rollback cleanup. An unknown remote orphan needs separate evidence bound to its backend and object key, recorded size/SHA, actual remote bytes, and absence of every `MediaObject`, direct/held image association and occupying receipt. Preserve uncertain objects and retain before/after evidence for any authorized exact-key operation.

Never bulk-delete a prefix and never delete an unknown remote object merely because it is absent from the current database. When identity or ownership is uncertain, preserve the object. During this hardening phase, R2 production write and cleanup are **NOT RUN**.
