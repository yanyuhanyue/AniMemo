# External Collection Sync

## Scope and domain boundary

Phase D0 establishes provider-neutral collection sync state, canonical values, a pure three-way planner, and a server-authoritative read-only preview. Phase D1A adds explicit field-level Pull and equal-value baseline confirmation. It never writes a provider collection, schedules work, or synchronizes episode progress.

The three external concepts remain independent:

- `ExternalMediaIdentity` identifies the provider subject bound to one `JournalEntry`.
- metadata source controls provider-owned title, studio, episodes, poster, and related descriptive fields.
- `UserExternalAccountConnection` identifies the user's provider account and protects its credentials.
- `ExternalCollectionSyncState` stores only confirmed common baselines for the user's `watch_status`, `personal_score`, and `review`.

`WatchHistoryRecord`, tags, visibility, metadata, episode progress, and analytics are outside the collection sync domain. Metadata refresh can update safe metadata fields but never reads or advances a collection baseline.

```text
JournalEntry
  |
  +-- ExternalMediaIdentity
  |     +-- provider subject
  |
  +-- ExternalCollectionSyncState
        +-- baseline: watch_status
        +-- baseline: personal_score
        +-- baseline: review

User
  |
  +-- UserExternalAccountConnection
        +-- Provider Account Adapter

Local Now ----+
              +---- Three-Way Planner <---- Confirmed Baseline
Remote Now ---+
```

## Baseline and lifecycle

A baseline is the **last confirmed common semantic value**: a value that AniMemo and the provider were both confirmed to hold after a successful explicit sync or initialization. It is not the most recently observed local value, the most recently observed remote value, or a timestamp inference.

Baseline schema version 1 stores at most these fields:

```json
{
  "watch_status": {"present": true, "value": "watching"},
  "personal_score": {"present": false, "value": null},
  "review": {"present": true, "value": ""}
}
```

Missing and present-empty are different. `personal_score` uses a canonical decimal string so `8`, `8.0`, and `8.00` compare as `"8"`. Baselines are schema-validated, canonically encoded, and bounded to 32 KiB; a review participating in sync is bounded to 10,000 Unicode code points.

There is one sync-state row per identity. The identity owner and provider must match the connection owner and provider. Deleting either the identity or connection cascades the state row. Credential refresh and token rotation preserve the same connection row and baseline. Disconnect followed by reconnect creates a new connection row, so the old baseline is deleted and cannot be reused. Migration `journal.0005` does not backfill or synthesize any baseline for existing data.

## Canonical fields

### watch_status

The provider-neutral values are `planned`, `watching`, `completed`, `on_hold`, and `dropped`. The Bangumi adapter alone maps its numeric types 1 through 5 to those values. `on_hold` and `dropped` remain distinct.

### personal_score

AniMemo permits decimal scores from 0 through 10. Canonicalization preserves the exact semantic decimal and never rounds. Bangumi's write schema accepts only integer values 0 through 10, with 0 meaning deletion. Consequently:

- a missing local score is representable as a future clear operation;
- a local score from 1 through 10 is representable only when integral;
- a fractional score such as 8.5 yields `push_supported=false` and `fractional_score_not_supported`;
- a present local score of 0 yields `zero_score_represents_clear`, because pushing 0 would lose the distinction between a present zero and a missing score.
- a present score with local `planned` status yields `planned_status_forces_score_clear`, because Bangumi's current domain implementation forces ratings on wish collections to 0. D1 must re-evaluate capabilities for the complete selected field action set.

### review

Canonical review text is NFC-normalized without newline rewriting or lossy truncation. AniMemo's model is an unbounded text field; D0 rejects content beyond its 10,000-code-point sync safety limit. The current Bangumi implementation trims surrounding whitespace, NFC-normalizes, permits newline/tab/carriage-return separators, rejects other non-printable content, and accepts at most 380 Unicode characters. A longer local review therefore yields `review_exceeds_provider_limit`, and unsupported control content yields `review_contains_unsupported_characters`; neither case is truncated. Remote `null`/missing and remote `""` remain distinct in the normalized snapshot.

Tags and episode progress are explicitly deferred. Similar names do not establish compatible semantics, and Bangumi episode state cannot supply the date semantics required by `WatchHistoryRecord`.

## Three-way planning

The planner imports no provider adapter or provider enum. It compares each canonical field independently:

| Local vs base | Remote vs base | Local vs remote | State |
| --- | --- | --- | --- |
| equal | equal | equal | `in_sync` |
| changed | equal | different | `local_changed` |
| equal | changed | different | `remote_changed` |
| changed | changed | equal | `converged` |
| changed | changed | different | `conflict` |

Without a baseline, equality produces `uninitialized_equal`; otherwise the state is `uninitialized`. This is information for a future explicit initialization, not permission to create a baseline. If the remote collection does not exist, every field is `remote_missing` and Pull is unavailable. Unsupported values use a stable block reason and cannot be silently coerced.

Every plan includes SHA-256 fingerprints of versioned canonical JSON for local, remote, and baseline snapshots. Fingerprints contain no credential or raw provider response. Conflict is always derived from the current three values and is never persisted as a flag. Neither `JournalEntry.updated_at` nor `ExternalMediaIdentity.updated_at` participates in planning.

Recommended actions are state- and capability-derived. D1A executes only `pull_remote` and `accept_equal`; `push_local` remains future planner vocabulary and `push_supported` does not mean a write client exists.

## Read-only preview

Authenticated clients call:

```text
GET /api/external-sync/providers/{provider}/entries/{entry_id}/preview/
```

The URL supplies only the provider and entry reference. AniMemo authoritatively resolves the owned entry, matching identity, same-user/same-provider connection, decrypted credential, live remote collection, current local values, and existing baseline. Client-provided local, remote, baseline, user, URL, or provider payload values are not accepted.

The fixed provider adapter performs the remote GET outside any database transaction or row lock. After the response returns, AniMemo revalidates entry ownership and deletion state, identity primary key/provider/external ID, connection primary key/provider/external user/owner/status, and the active entry-provider binding. Any change produces HTTP 409 with `sync_context_changed`; an old subject response can never be attached to a replacement binding. A disconnected/reconnected connection likewise invalidates the old response.

Cross-user targets return the same 404 `sync_target_not_found` response as absent targets. A connection already needing authorization, or one receiving provider 401/403, is denied with `external_account_needs_reauthorization`; provider 401/403 also marks the Phase C connection for reauthorization. URLs remain fixed inside the adapter. The response contains normalized snapshots, plan metadata, and a short-lived signed `preview_token`, never credentials or the raw provider document. The token uses Django timestamp signing with a provider-neutral salt and `EXTERNAL_SYNC_CONFIRMATION_MAX_AGE_SECONDS` (default 300 seconds). It contains only schema/context IDs plus local, remote, and baseline fingerprints. It contains no field values, review text, score, status, credential, or provider payload.

Preview does not mutate `JournalEntry`, `WatchHistoryRecord`, provider collection data, or `ExternalCollectionSyncState`. Even `uninitialized_equal` does not create a row.

## Official Bangumi outbound contract audit

Verified on **2026-08-09** against official sources only:

- `bangumi/server` commit [`10084d67069e6de6275b085775987cf8f9c708e1`](https://github.com/bangumi/server/tree/10084d67069e6de6275b085775987cf8f9c708e1)
- official OpenAPI write routes and bearer scope: [`openapi/v0.yaml`](https://github.com/bangumi/server/blob/10084d67069e6de6275b085775987cf8f9c708e1/openapi/v0.yaml#L1165-L1249)
- official payload schema: [`user_subject_collection_modify_payload.yaml`](https://github.com/bangumi/server/blob/10084d67069e6de6275b085775987cf8f9c708e1/openapi/components/user_subject_collection_modify_payload.yaml)
- official status enum: [`subject_collection_type.yaml`](https://github.com/bangumi/server/blob/10084d67069e6de6275b085775987cf8f9c708e1/openapi/components/subject_collection_type.yaml)
- actual validation and normalization: [`web/req/collection.go`](https://github.com/bangumi/server/blob/10084d67069e6de6275b085775987cf8f9c708e1/web/req/collection.go)
- actual PATCH/POST handlers: [`patch_subject_collection.go`](https://github.com/bangumi/server/blob/10084d67069e6de6275b085775987cf8f9c708e1/web/handler/user/patch_subject_collection.go), [`post_subject_collection.go`](https://github.com/bangumi/server/blob/10084d67069e6de6275b085775987cf8f9c708e1/web/handler/user/post_subject_collection.go)
- OAuth bearer transport from official `bangumi/api` commit [`65d29cff2331e08c0110d30d515f7f1b6488f845`](https://github.com/bangumi/api/blob/65d29cff2331e08c0110d30d515f7f1b6488f845/docs-raw/How-to-Auth.md)

Audited semantics:

- `POST /v0/users/-/collections/{subject_id}` creates or modifies; `PATCH` modifies an existing collection.
- both require bearer authorization with `write:collection` in the OpenAPI contract.
- type is integer 1..5: wish/done/doing/on-hold/dropped.
- rate is integer 0..10; 0 removes the rating. A wish collection forces rate to 0 in the current server domain implementation.
- comment is trimmed, NFC-normalized, printable, and at most 380 Unicode characters; an explicitly supplied empty comment clears it. An omitted field is ignored by the patch structure.
- PATCH documents and implements HTTP 204. The OpenAPI contract documents POST as 204, while the current handler returns HTTP 202. No ETag or conditional collection write mechanism is documented.
- the implementation defaults to 3,000 requests per 10-minute window and may impose a longer ban, but this is server configuration rather than a stable public client quota.

**Contract verdict: NOT VERIFIED for a complete D1B Push protocol.** Field representation and PATCH update semantics are verified, but the official POST success-code contradiction and absence of a conditional-write contract prevent claiming a complete create/update protocol. Phase D1B Push remains **HOLD** until those points are resolved and locked in tests. D1A only uses the existing collection GET endpoint and implements no collection write method.

## Phase D1A explicit manual Pull

D1A is implemented through provider-neutral endpoints:

```text
GET  /api/external-sync/providers/{provider}/entries/{entry_id}/preview/
POST /api/external-sync/providers/{provider}/entries/{entry_id}/apply/
```

The Apply body contains only the signed preview token and at most one action per supported field:

```json
{
  "preview_token": "server-signed-value",
  "actions": [
    {"field": "watch_status", "action": "pull_remote"},
    {"field": "review", "action": "accept_equal"}
  ]
}
```

Unknown or duplicate fields, unknown actions, `push_local`, extra values, arbitrary baselines, and empty/all-skip requests are rejected before mutation. Omitted fields are `SKIP`. The UI's **保留 AniMemo** choice is also `SKIP`: it changes neither local nor remote data, baseline, nor `last_synced_at`.

### Action matrix

| Current state | D1A actions |
| --- | --- |
| `in_sync` | no action |
| `uninitialized_equal`, `converged` | `accept_equal` |
| `uninitialized`, `remote_changed`, `local_changed`, `conflict` | `pull_remote` or skip |
| `remote_missing`, `unsupported` | skip only |

`accept_equal` never changes `JournalEntry`; it verifies current canonical equality and advances only that field's baseline. `pull_remote` validates lossless local representability, writes only `watch_status`, `personal_score`, or `review`, verifies the resulting canonical local value equals the fresh remote value, and then advances only selected baselines.

`watch_status` supports all five canonical states. A present Bangumi integer score maps exactly to `Decimal`; a missing score maps to `None`. A present empty review maps to `review=""` and its baseline remains `present=true`. `JournalEntry.review` cannot represent provider-level missing independently from empty, so a missing remote review is `unsupported` with `remote_value_not_representable`. D1A never truncates review text, rounds a score, collapses missing into empty, or guesses an enum.

### Confirmation and stale protection

Apply verifies the signed token and request binding before resolving the current owned entry, identity, and account connection. It decrypts the credential and performs exactly one fresh collection GET outside a database transaction. It then rechecks the context and opens a short transaction that locks the entry, identity, connection, and existing sync state. Current canonical snapshots and the planner are recomputed under those locks.

Any local, remote, or baseline fingerprint mismatch returns HTTP 409 `sync_preview_stale` without mutation. Identity rebind, external ID change, disconnect/reconnect, owner/provider mismatch, or replacement connection returns `sync_context_changed`; a connection requiring authorization returns `external_account_needs_reauthorization`. Malformed signatures, expiry, and changed data use distinct `sync_preview_invalid`, `sync_preview_expired`, and `sync_preview_stale` codes. The frontend refreshes stale previews but never automatically retries Apply.

### Baseline write invariant

All application creation, baseline updates, and `last_synced_at` changes pass through `journal.external_sync.state`. The service runs only inside the locked transaction and refuses to advance any selected field unless canonical local equals canonical remote. Existing unselected baselines are copied unchanged. A first Apply can therefore create a partial state containing only `watch_status`; score and review remain absent rather than receiving synthetic values.

No state row is created for skip-only requests. `last_synced_at` is updated only when at least one selected field successfully advances a confirmed common baseline. Local changes and baseline advancement are committed atomically for the whole request.

Apply reports `sync_state_initialized` from the final baseline state and separately reports `sync_state_created` when this request created the row.

### UI and rate boundary

The comparison entry appears in the bound work's external-identity panel only when the same provider account is connected and preview, Pull, and Apply capabilities are enabled. Opening the editor does not contact Bangumi; the remote GET starts only after **比较 Bangumi 收藏**. Each field shows AniMemo, Bangumi, useful prior-baseline context, a textual state, and only the allowed **使用 Bangumi**, **确认当前一致**, or **保留 AniMemo** choices. The panel states that it is Pull-only and cannot modify Bangumi.

Preview and Apply use separate provider-neutral DRF throttle scopes backed by the existing cache. There is no Redis preview session, queue, worker, scheduler, polling loop, or automatic Apply.

## Phase D1B boundary

Manual Push remains deferred. Bangumi currently documents no conditional write/ETag, and the audited POST success contract remains contradictory (OpenAPI 204 versus current handler 202). D1B may begin only after the complete outbound contract is formally accepted and locked in tests. D1A contains no hidden write flag and adds no collection mutation method.

## Deferred work

- Bangumi collection writes and Manual Push: D1B, on hold pending the complete outbound contract.
- Automatic/background/periodic sync: Phase D2 or later.
- Episode identity and progress sync: separate future domain.
- WatchHistory provider sync, tags, and annual-report work: deferred.
- Production deployment: outside Phase D1A and not run by this change.
