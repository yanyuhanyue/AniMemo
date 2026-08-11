# Media Storage Pool

Production media uses `site_config.media_storage.storage.StoragePoolStorage`. The database-backed pool supports `cloudflare_r2` and `local` backends. New writes use the preferred backend while it remains writable; otherwise enabled backends are tried in ascending `priority` order. A successful failover becomes the preferred backend, so a recovered backend is not automatically selected again.

Every production upload creates a `site_config.models.MediaObject` row. Existing `ImageField` values contain a stable `media-objects/<uuid>` reference, and the row records the original backend plus object key. Reads, URLs, deletes, and replacements resolve through that row rather than guessing from the current preferred backend.

R2 credentials are encrypted with `config.credentials.CredentialCipher` using the independent `CREDENTIAL_ENCRYPTION_KEY`. The API exposes only `*_configured` flags. R2 client caches are keyed by `config_version`, so a backend edit is picked up by every worker on its next operation without a restart.

Cloudflare usage snapshots are observability data only. The hard write guard uses the strongly consistent sum of `MediaObject.size_bytes` for the selected backend and conservatively takes `max(managed_usage, last_known_cloudflare_snapshot)`. There is no Redis snapshot/delta reset race. Local backends use `shutil.disk_usage`, reserve `min_free_warning_bytes` / `min_free_block_bytes` for the operating system and services, and include the incoming upload size before writing. Write blocking never prevents reads or cleanup.

R2 thresholds use decimal GB (`1 GB = 1,000,000,000 bytes`). Local disk reserves use GiB (`1 GiB = 1,073,741,824 bytes`) and are labeled as GiB in the admin UI. Optional `CloudflareR2Account` budgets aggregate managed bytes and refreshed bucket snapshots across all linked buckets, so two buckets in one account share one configured account limit; accounts without a configured limit do not add a guard.

For periodic external usage refresh, run the built-in command from a host scheduler every 30–60 minutes as appropriate for the deployment. On the production VPS, invoke it through the API container from the real Compose working directory:

```bash
cd /opt/1panel/docker/compose/anime-journal/app
docker compose --env-file .env.production -f deploy/docker-compose.yml \
  exec -T api python manage.py refresh_media_storage_usage
```

The command reports only backend slugs and `success/failed/skipped` counts. It never prints access keys, secrets, analytics tokens, or authorization headers. One failing R2 refresh does not prevent the remaining backends from being attempted.

The media storage admin API and UI are Superuser-only:

- `GET/POST /api/v1/staff/system/media-storage/`
- `GET/PATCH/DELETE /api/v1/staff/system/media-storage/<id>/`
- `POST /api/v1/staff/system/media-storage/<id>/actions/`

Use `action=test-connection`, `refresh-usage`, `set-active`, `toggle-writes`, or `clear-credentials` for explicit operations. Production without any configured backend starts normally and returns `MEDIA_STORAGE_SETUP_REQUIRED` for new media writes; it never silently falls back to a local directory.
