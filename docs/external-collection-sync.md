# External Collection Sync

## Scope and domain boundary

Phase D0 establishes provider-neutral collection sync state, canonical values, a pure three-way planner, and a server-authoritative read-only preview. It does not apply remote values, write a provider collection, create a baseline, schedule work, or synchronize episode progress.

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

Recommended actions (`pull_remote`, `push_local`, `accept_equal`) only describe future representable choices. They never execute in D0, and `push_supported` does not mean a write client exists.

## Read-only preview

Authenticated clients call:

```text
GET /api/external-sync/providers/{provider}/entries/{entry_id}/preview/
```

The URL supplies only the provider and entry reference. AniMemo authoritatively resolves the owned entry, matching identity, same-user/same-provider connection, decrypted credential, live remote collection, current local values, and existing baseline. Client-provided local, remote, baseline, user, URL, or provider payload values are not accepted.

The fixed provider adapter performs the remote GET outside any database transaction or row lock. After the response returns, AniMemo revalidates entry ownership and deletion state, identity primary key/provider/external ID, connection primary key/provider/external user/owner/status, and the active entry-provider binding. Any change produces HTTP 409 with `sync_context_changed`; an old subject response can never be attached to a replacement binding. A disconnected/reconnected connection likewise invalidates the old response.

Cross-user targets return the same 404 `sync_target_not_found` response as absent targets. A connection already needing authorization, or one receiving provider 401/403, is denied with `external_account_needs_reauthorization`; provider 401/403 also marks the Phase C connection for reauthorization. URLs remain fixed inside the adapter. The response contains normalized snapshots and plan metadata only, never credentials or the raw provider document.

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

**Contract verdict: NOT VERIFIED for a complete D1 Push protocol.** Field representation and PATCH update semantics are verified, but the official POST success-code contradiction and absence of a conditional-write contract prevent claiming a complete create/update protocol. Phase D1 Push remains **HOLD** until those points are resolved and locked in tests. D0 only uses the existing GET endpoint and implements no collection write method.

## Phase D1 manual protocol

D1 may implement explicit per-field actions only. It must not add a scheduler, polling, or automatic synchronization.

### Manual Pull

1. Produce a read-only preview and show field choices.
2. Receive explicit user confirmation referencing server-issued fingerprints, never client-supplied values.
3. Re-resolve ownership, identity, connection, and baseline; read the remote collection again outside the transaction.
4. Open a short transaction, lock the entry/identity/state rows, revalidate context and local/baseline fingerprints, then apply only explicitly confirmed fields.
5. Persist the local change and the exact confirmed common baseline atomically.

### Manual Push

1. Produce a preview and receive explicit per-field confirmation tied to fingerprints.
2. Recheck context, local values, baseline, and capabilities; read the remote collection again.
3. Abort if the remote snapshot no longer matches the confirmed preview.
4. Perform the minimal outbound write only after the official write contract gate passes.
5. Fetch the remote collection again and verify exact canonical semantics; HTTP success alone is insufficient.
6. In a short transaction, lock and recheck local/context/baseline. Advance the baseline only if local and verified remote values are still common.
7. If local changes during the network operation, report `local_changed_during_sync` and do not advance the baseline.

Bangumi currently documents no conditional write/ETag. A remote writer can therefore change the collection between D1's final read and write. D1 must disclose this check-then-write race, minimize the window, verify after writing, and never represent the operation as globally serializable. Post-write verification can detect divergence but cannot erase an already-issued remote mutation.

## Deferred work

- Manual application and first baseline initialization: Phase D1.
- Bangumi collection writes: not implemented; D1 Push on hold.
- Automatic/background/periodic sync: Phase D2 or later.
- Episode identity and progress sync: separate future domain.
- WatchHistory provider sync, tags, and annual-report work: deferred.
- Production deployment: outside Phase D0.
