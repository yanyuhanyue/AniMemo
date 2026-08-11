# Architecture Contract Hardening Inputs

来源：AniMemo v1.0 Full Repository Audit，基线 `e68dc0ce5c51c90d7b419369a31255d51736e196`，审计后 main `a7c6eb3a73b5b3e26d58425567e1aa6dd3d33905`。

本文件只定义下一阶段的设计输入、约束与问题，不实施 API v1、Mobile、Plugin Runtime v3、Theme Contract 或 Marketplace。

## 1. API Versioning

Current evidence:

- Core routes directly use `/api/` in `backend/config/urls.py` and `backend/journal/urls.py`.
- Integration Protocol already uses `/api/integrations/v1/` and demonstrates an explicit frozen contract.
- Web code builds relative endpoint strings throughout `src/`.

Required decisions:

- Whether Core v1 is `/api/v1/` or negotiated another way.
- Compatibility window and deprecation policy for existing `/api/` URLs.
- Version ownership for auth, staff, public showcase, plugins and dynamic plugin routes.
- Whether error codes and DTO fields are independently versioned from route paths.

Hard constraints:

- No flag-day break for the production web client.
- Stateful upgrade must preserve existing database and persisted plugin identities.
- Integration Protocol v1 remains frozen unless a v2 is explicitly introduced.

## 2. Auth Transport Separation

Current evidence:

- Access token is memory-only in `src/lib/api.js`.
- Refresh token is HttpOnly cookie-only and all cookie mutations require CSRF.
- Refresh rotation, blacklist/revocation, session version, password reset/change and 2FA are Core security semantics.

Required contract split:

- Auth Core: authenticate, rotate, revoke, session version, 2FA/recovery, device/session metadata.
- Web Adapter: HttpOnly cookie, CSRF bootstrap, same-origin credentials and browser logout.
- Future Native Adapter: OS secure storage, bearer refresh transport or another explicitly threat-modeled mechanism.

Hard constraints:

- Native support must not weaken web CSRF or expose refresh tokens to JavaScript.
- Refresh replay/rotation must remain single-success under PostgreSQL concurrency.
- Logout/password/account actions must keep deterministic revocation semantics.

## 3. API Transport Separation

Current evidence:

- `src/lib/api.js` combines base URL discovery, browser cookie behavior, auth state, CSRF, retry, error parsing and server-state invalidation.
- Reusable UI/domain helpers still reach `window`, `document`, `location` and browser storage.

Required modules:

- Transport interface: request/response, cancellation, timeout, credentials.
- Auth session interface: access acquisition/refresh/logout/subscription.
- Error core: stable `code`, `detail`, `fields`, `retryAfterSeconds`, status.
- Server-state notification interface independent from axios and DOM.
- Web adapter providing browser URL/cookie/CSRF/navigation services.

Acceptance input:

- Domain/API tests can run without DOM globals.
- Existing web request, refresh-sharing and invalidation behavior remains covered.

## 4. Service Boundaries

Current evidence:

- Core Journal/Watch History/Analytics capabilities return DTOs and bind an authenticated actor.
- Some plugin/staff view modules still perform list serialization and workflow coordination directly.
- Official importer needs direct transaction/storage/time/network facilities beyond current Core capabilities.

Required boundaries:

- Keep owner/security decisions server-authoritative.
- Move only reusable business invariants into services; do not create thin wrappers for every ORM call.
- Define transaction ownership for multi-domain mutation and on-commit side effects.
- Define query services only where they provide bounded/paginated contracts or remove measured amplification.

Hard constraints:

- No generic plugin database capability.
- No arbitrary `user_id` accepted by actor-bound capabilities.
- No Dashboard rewrite as a side effect of backend service work.

## 5. Plugin SDK Contract

Current evidence:

- SDK v2 provides actor-bound Journal, Watch History, Analytics and Integration capabilities.
- Official importer still imports Django settings, transaction/timezone and plugin_host storage/errors directly.
- Runtime/hook/action registrations use in-process callbacks and singleton registries.

Required SDK additions to evaluate:

- Bounded namespaced storage with retention and compare/lock semantics.
- Transaction/mutation unit contract or host-owned transactional batch API.
- Clock and structured logging interfaces.
- External network client with allowlist/timeouts/response limits.
- Declarative action/event/hook descriptors and structured error transport.
- Capability version negotiation independent from plugin package SemVer.

Hard constraints:

- Existing `slug + version` immutable package identity and CAS remain authoritative.
- Backend runtime publication stays superuser/trusted-publisher only until a new security model is approved.
- USER installation and actor binding must remain enforced on every capability call.

## 6. OpenAPI Contract

Current evidence:

- Core schema passes `spectacular --validate --fail-on-warn`.
- Dynamic plugin dispatch is intentionally outside the static Core schema.
- Error codes are documented but not all domain-specific alternatives are machine-enumerated.

Required decisions:

- Core v1 schema version/artifact naming.
- Stable error-code catalog and endpoint-specific alternatives.
- Plugin-provided schema artifact format and namespace.
- Whether generated clients include plugin contracts dynamically or as separate packages.
- How compatibility gates compare Base and Current OpenAPI artifacts.

Hard constraints:

- No sensitive runtime/plugin diagnostics in public schemas.
- Dynamic schema registration cannot bypass package/review/runtime authorization.

## 7. Stable Resource Identity

Current evidence:

- Internal owner resources commonly use numeric database IDs.
- Public showcase/share surfaces use UUID slugs.
- Integration actions use explicit request IDs; importer additionally needs a business batch identity.
- Official plugins use immutable `slug + version` plus content/blob hashes.

Required contract:

- Distinguish internal primary key, public identifier and idempotency key.
- Define whether client-visible IDs may be cached/offline and survive export/import.
- Define mutation idempotency scope and retention.
- Define plugin/integration identity references without exposing protected metadata.

Hard constraints:

- Do not rewrite existing production IDs or destructive-migrate Core data.
- Owner isolation remains independent from identifier unpredictability.

## 8. Mobile Assumptions

Inventory to remove from shared layers:

- Cookie-only refresh and CSRF acquisition.
- `window.location.origin` API discovery.
- DOM navigation, `document` events and browser storage in reusable modules.
- Same-origin frontend plugin loading.
- Web-only download/upload affordances.

Inputs required before implementation:

- Mobile threat model and secure storage choice.
- Device/session revocation UX.
- Offline/cache policy for Journal and Watch History.
- Background sync constraints and conflict semantics.
- Public/deep-link identity strategy.

Explicit non-goal for this phase: creating an Expo/React Native project.

## 9. Integration and Event Contract

Current evidence:

- Protocol v1 HMAC, nonce, timestamp, idempotent action receipt, cursor and ACK semantics are strong.
- Idle long-poll holds synchronous workers and queries PostgreSQL every 250 ms.
- Events and completed/failed receipts now have retention.

Required design inputs:

- Preserve wire-compatible long-poll while adding broker/notification wakeup if possible.
- Define delivery backlog, dead-letter and maximum retention semantics.
- Define observable lag/queue metrics without leaking routes or user identifiers.

## 10. Plugin Runtime v3 Inputs

Migration blockers:

- `importlib`/`sys.modules` in-process loading.
- Direct Django/settings/ORM/storage imports.
- Process-local hook/event/action callbacks.
- Runtime filesystem path assumptions.
- Host objects passed directly instead of serializable RPC messages.

Future platform inputs to record, not implement here:

- Trusted Publisher and Trusted Runtime Publisher identities.
- Source repository + exact commit provenance.
- Canonical content digest and package SHA-256.
- Review/signing/revocation and rollback policy.
- Worker/container resource, filesystem, network and secret boundaries.

## 11. Theme Contract Inputs

Current evidence:

- `src/styles.css` contains 4,159 lines of global/page-specific presentation.
- Plugin styles are injected into the same document.
- Current production UI is intentionally frozen for v1.0.

Required design inputs:

- Primitive, semantic and component token layers.
- Typography, color, spacing, border, shadow, motion and focus contracts.
- Component variants and semantic plugin extension slots.
- Compatibility approach for existing saved tag colors and production-matched pages.

Hard constraints:

- Contract work must not silently become a visual redesign.
- Dashboard behavior and data flow remain unchanged until explicitly unfrozen.

## 12. Verification Contract for the Next Phase

Every Architecture Contract Hardening PR should identify:

- Base/current API or behavior artifact.
- Compatibility and rollback statement.
- Runtime tests plus static schema/contract tests.
- Migration plan (`NOT APPLICABLE` unless additive schema is necessary).
- PR CI, Release Gate and post-merge results.
- Explicit confirmation that production deployment was not performed.

The next phase is complete only when contracts are written, tested and adopted by the current web client without opening P0/P1 regressions. It is not complete merely because new directory names or adapters exist.
