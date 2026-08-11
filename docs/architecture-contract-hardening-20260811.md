# AniMemo Architecture Contract Hardening

Contract date: `2026-08-11`

## Executive Summary

Architecture Contract Hardening freezes the AniMemo v1.0 public boundaries without implementing Mobile, Plugin Runtime v3 isolation, Marketplace expansion, UI/UX 2.0 or Theme Contract. The work establishes one canonical `/api/v1/` contract, machine-readable OpenAPI/error schemas, stable resource identities, reusable API/Auth cores, Web adapters, Core-owned domain services and an explicit Plugin SDK capability contract.

The existing Web product, official plugin, backend/frontend plugins, Integration Protocol v1 and AstrBot Bridge remain compatible. No production deployment, production smoke, database migration, R2 write or infrastructure restart is part of this phase.

## Baseline

```text
EXPECTED BASE MAIN SHA:
bbff1354f235a180a48c3f216b94c8b295f1cd96

LAST KNOWN GOOD PRODUCTION:
bbff1354f235a180a48c3f216b94c8b295f1cd96

BATCH D BASE MAIN SHA:
db85e08a77de5e6bf3a5719d96c9396ee44dc04f
```

The legal main advance from the production baseline was audited before each batch. Contract PRs completed before Batch D:

| Batch | PR | Title | Merge SHA | Result |
| --- | --- | --- | --- | --- |
| A | #46 | 建立 API v1 与 OpenAPI 稳定契约 | `dd35dbf6f5b6e88e1624c3ddbb6326dd60535d01` | PASS |
| B | #48 | 拆分 Web API 传输与认证适配边界 | `d988511cfa04561448bc81fc49d3eff10e01e8a4` | PASS |
| C | #49 | 稳定领域服务与 Plugin SDK 契约 | `db85e08a77de5e6bf3a5719d96c9396ee44dc04f` | PASS |

## API v1, OpenAPI And Errors

- `/api/v1/` is the canonical Core client contract.
- Existing `/api/` routes remain compatibility aliases backed by the same URL table, Views, Serializers, permissions and services. New legacy-only endpoints are forbidden.
- `/api/integrations/v1/` remains the independently frozen Integration Protocol v1.
- Core OpenAPI publishes only canonical v1 and Integration v1 paths, validates without warnings and excludes dynamic plugin-owned routes intentionally.
- JSON errors use stable `code`, human-readable `detail`, optional `fields`, optional non-sensitive `metadata` and retry metadata where applicable.
- Page-number pagination remains `{count, next, previous, results}`; v1 query semantics cannot be replaced silently.

Detailed inventory: `docs/api-v1-contract.md`.

## Stable Resource Identity

- User-owned Core resources retain stable integer database IDs.
- Public journal, shared entry and public column links use immutable UUID slugs.
- External import sessions, Integration connections and Media objects use UUID identity.
- Plugin release identity is immutable `slug + version`; canonical content identity and archive SHA remain separate.
- Integration mutation identity is `connection + request_id`.
- Media object keys use immutable owner IDs and random UUIDs, not username, display name or mutable title.

No identity migration is required. Identifier unpredictability never replaces owner isolation.

## API Core And Web Transport

| Module | Responsibility | Browser dependency |
| --- | --- | --- |
| `src/lib/apiCore.js` | v1 path, query/error/challenge normalization | Forbidden |
| `src/lib/authSession.js` | in-memory access/user state and subscription | Forbidden |
| `src/lib/webApiTransport.js` | Axios, credentials, Bearer injection, retry and mutation notification | Web-only by design |
| `src/lib/webAuthAdapter.js` | CSRF, HttpOnly-cookie refresh flow and legacy browser-token cleanup | Web-only by design |
| `src/lib/api.js` | Web composition facade and compatibility exports | Web-only by design |

ESLint now rejects browser globals and Web/React/transport imports in the two shared Core modules. Runtime Node tests import and execute them without DOM globals.

## Auth Core, Web Adapter And Anti-Abuse

- `auth_tokens.py` owns raw token issuance, validation, rotation, replay defense, revocation and session-version semantics.
- Token Core accepts raw credentials; it does not read Django/DRF requests, write cookies or construct HTTP responses.
- `web_auth_adapter.py` owns Bearer extraction, refresh-cookie attributes, CSRF/challenge request extraction and response cookie/cache headers.
- Refresh replay auditing remains in authenticated HTTP orchestration without changing the token transaction.
- Web retains memory-only access tokens, HttpOnly refresh cookies, CSRF, refresh rotation, logout, 2FA and staff semantics.
- `AntiAbuseChallenge {provider, token}` is provider-neutral. Turnstile remains the Web provider adapter and its legacy request alias remains deprecated-compatible.
- Future Mobile refresh transport can be added as an adapter without changing token Core or weakening Web CSRF.

Detailed contract: `docs/auth-contract.md`.

## Domain Service Boundary

`JournalEntryService` owns owner-scoped list/get, shared DTO representation, allowed plugin fields, serializer validation, create/update mutations and journal mutation Hook dispatch. Web Views and Plugin capabilities use the same service. Existing Watch History, Analytics, External Media, External Accounts, External Sync and Data Bundle services continue to own their established domains.

Views, plugins and Integration orchestration depend on services/capabilities; plugins do not call Django Views. No generic plugin database capability exists.

## Plugin SDK And Runtime v3 Boundary

- Manifest v2 explicitly declares `coreCapabilities` for `journal`, `watch_history` and `analytics`.
- Undeclared Core capabilities, storage and settings are rejected by the Host.
- Actor binding and enabled USER installation are checked on every capability call.
- Official plugins import Host errors/storage-limit types through `plugin_host.sdk`, not runtime/storage private modules.
- Core owns User, JournalEntry, Watch History, plugin settings, plugin storage and media object identity.
- Runtime-local filesystem, memory or container-local databases are not valid persistent data contracts.
- In-process callbacks, Django transaction/time/network assumptions and worker/container isolation remain explicit Runtime v3 implementation work.

Detailed contracts: `docs/plugin-sdk-contract.md`, `docs/plugin-runtime-v3-boundary.md` and `docs/domain-service-boundary.md`.

## Mobile API Readiness

Mobile implementation is deferred, but the v1 contract can support a future client without importing Web UI or Web transport modules:

| Requirement | Evidence | Status |
| --- | --- | --- |
| Stable API version | Canonical `/api/v1/`, legacy alias parity gate | PASS |
| Stable resource IDs | Model/constraint/upload-key contract tests | PASS |
| Machine-readable errors | Canonical renderer and OpenAPI `ApiError` | PASS |
| Stable pagination/search/filter | API v1 contract and Dashboard server-query regression | PASS |
| Auth Core not cookie-owned | Raw token Core plus Web cookie adapter | PASS |
| Shared client Core not browser-owned | ESLint boundary plus Node runtime tests | PASS |
| Domain rules server-authoritative | Domain services and actor-bound capabilities | PASS |
| OpenAPI usable | Warning-free canonical schema with auth/error/pagination metadata | PASS |

Future Mobile Auth Adapter requirements:

- access token in process memory;
- refresh credential in iOS Keychain, Android Keystore or equivalent secure storage;
- no AsyncStorage/localStorage for refresh credentials;
- Bearer/native refresh transport implemented as a separate adapter;
- mobile-compatible challenge provider added without changing Auth Core.

## Web-only Assumption Inventory

| Dependency | Classification | Decision |
| --- | --- | --- |
| Browser origin, cookies, CSRF, Axios credentials | Correctly Web-only transport | Keep in Web adapters |
| `window`, `document`, focus, hover, animation, React Router | Correctly Web-only presentation | Keep in Web application |
| Browser upload/download affordances | Correctly Web-only product UI | Mobile UX deferred |
| Same-origin frontend plugin import/style injection | Trusted Web Plugin Runtime | Sandbox/isolation deferred |
| API paths, errors, challenge normalization, session state | Shared Core | Browser dependencies forbidden |
| Token validation/rotation/revocation | Backend Auth Core | HTTP request dependencies forbidden |

No incorrectly placed browser dependency remains in the shared API/Auth Core.

## Architecture Dependency Direction

```text
                    AniMemo Core
                         |
        +----------------+----------------+
        |                |                |
   Domain Services   Auth Core       API Contract
        |                |                |
        +----------+-----+-------+--------+
                   |             |
             Web Adapters   Plugin Contract
                   |             |
                Web App      Plugin Runtime
                                  |
                         Future Worker/Container

                   API Contract
                         |
                   Future Mobile

Integration Gateway -> Domain/Plugin Contract
AstrBot Bridge      -> Integration Protocol v1
```

Automated AST gates protect the critical directions:

- Auth Core cannot import Web/Auth Views or DRF response/view adapters and cannot accept a `request` parameter for token rotation/revocation.
- Domain Service cannot import Journal Views, DRF Views/ViewSets or Plugin Runtime.
- Official plugin cannot import Core Journal modules or private `plugin_host.*` modules outside `plugin_host.sdk`.
- Shared frontend Core cannot import Web transport/UI libraries or reference browser globals.

## Compatibility Matrix

| Client/runtime | API version | Auth method | Data contract | Extension contract | Status |
| --- | --- | --- | --- | --- | --- |
| Web | `/api/v1/`, legacy aliases retained | Bearer access + HttpOnly refresh cookie + CSRF | OpenAPI/Core DTOs | Web adapters and frontend Host SDK | PASS |
| Official plugin | Host capabilities over Core v1 semantics | Actor-bound enabled installation | Core Journal/Watch History DTOs, Core storage/settings | Plugin SDK v2, Manifest v2 | PASS |
| Backend plugin | Dynamic `/api/v1/plugins/...` | Manifest access + actor/staff policy | Host capability DTOs | Trusted in-process Runtime, SDK v2 | PASS |
| Frontend plugin | Host-relative v1 client | Read-only Web auth snapshot | Plugin-owned UI over Host APIs | Same-origin trusted frontend Runtime | PASS |
| Integration | `/api/integrations/v1/` | Pairing Bearer or HMAC v1 | Frozen action/event/receipt DTOs | Provider-neutral Integration contract | PASS |
| AstrBot Bridge | `/api/integrations/v1/` | HMAC v1 | Frozen Integration DTOs | Bridge v1 | PASS |
| Future Mobile | `/api/v1/` | Future native adapter + secure refresh storage | OpenAPI/Core DTOs and stable errors | No Web or Plugin Runtime dependency required | PASS |

## Contract Enforcement

| Contract | Gate |
| --- | --- |
| API v1 and legacy parity | Django route/callback/runtime contract tests |
| OpenAPI/error/auth schemas | `spectacular --validate --fail-on-warn`, schema tests |
| Stable identity | Model/constraint/upload-key contract tests |
| Frontend Core portability | ESLint restricted globals/imports, Node runtime tests |
| Backend dependency direction | Python AST architecture tests |
| Auth/security semantics | Auth heavy regression and PostgreSQL refresh concurrency |
| Plugin capability/Manifest | Runtime tests, schema validation and static plugin validator |
| Official Plugin SDK surface | Python AST test, package validate/build/immutability/pack |
| Dashboard compatibility | Existing query/mutation tests and critical E2E |
| Database compatibility | `makemigrations --check`, stateful-upgrade Release Gate |

## Deferred Work

The original Architecture Audit findings `AUD-P2-001` through `AUD-P2-003` are closed by API versioning, Auth/Web separation and shared Core portability. The following 13 P2 items remain deliberately deferred:

1. `AUD-P2-004` Dashboard controller decomposition after the v1.0 freeze.
2. `AUD-P2-005` Theme/UIUX token contract and bundle optimization.
3. `AUD-P2-006` Dynamic plugin OpenAPI artifact/aggregation.
4. `AUD-P2-007` Frontend plugin sandbox or isolated origin.
5. `AUD-P2-008` Backend Plugin Worker/Container isolation.
6. `AUD-P2-009` Host clock, transaction and network contracts replacing remaining direct Django/process assumptions.
7. `AUD-P2-010` Durable hook/event/RPC transport across runtime processes.
8. `AUD-P2-011` Integration long-poll broker/async wakeup and queue metrics.
9. `AUD-P2-012` AstrBot pending-event dead-letter/expiry policy.
10. `AUD-P2-013` Marketplace/developer list pagination for future scale.
11. `AUD-P2-014` Additional low-maintenance critical browser journeys.
12. `AUD-P2-015` Gradual replacement of structural source tests with runtime behavior tests.
13. `AUD-P2-016` Broader ESLint/Ruff correctness rules after scoped measurement.

Explicit roadmap status:

```text
MOBILE IMPLEMENTATION: DEFERRED
PLUGIN RUNTIME V3 IMPLEMENTATION: DEFERRED
PLUGIN CONTAINER: DEFERRED
PLUGIN MARKETPLACE EXPANSION: DEFERRED
UI/UX 2.0: DEFERRED
THEME CONTRACT IMPLEMENTATION: DEFERRED
.AJTHEME: DEFERRED
```

## Acceptance Matrix

```text
API V1 CONTRACT: PASS
OPENAPI CONTRACT: PASS
ERROR CONTRACT: PASS
RESOURCE IDENTITY: PASS
API CORE: PASS
WEB TRANSPORT: PASS
AUTH CORE: PASS
WEB AUTH ADAPTER: PASS
ANTI-ABUSE BOUNDARY: PASS
DOMAIN SERVICE BOUNDARY: PASS
PLUGIN SDK CONTRACT: PASS
PLUGIN CAPABILITY CONTRACT: PASS
PLUGIN RUNTIME V3 BOUNDARY: PASS
MOBILE API READINESS: PASS
ARCHITECTURE DEPENDENCY DIRECTION: PASS
COMPATIBILITY: PASS

NEW MIGRATION: NOT APPLICABLE
DATABASE PRODUCTION CHANGE: NOT APPLICABLE
R2 PRODUCTION CHANGE: NOT APPLICABLE
PLUGIN PRODUCTION CHANGE: NOT APPLICABLE
BRIDGE PRODUCTION CHANGE: NOT APPLICABLE
ASTRBOT CHANGE: NOT APPLICABLE
NAPCAT CHANGE: NOT APPLICABLE
OPENRESTY CHANGE: NOT APPLICABLE
CLOUDFLARE CHANGE: NOT APPLICABLE
DOCKER GLOBAL CHANGE: NOT APPLICABLE

PRODUCTION DEPLOY: NOT RUN
PRODUCTION SMOKE: NOT RUN

ARCHITECTURE P0 OPEN: 0
ARCHITECTURE P1 OPEN: 0
ARCHITECTURE P2 DEFERRED: 13
```

## Final Verification

Local full regression completed on 2026-08-11:

- `npm ci`, `npm run lint`, `npm run build`: PASS.
- `npm test`: 147 passed.
- `npm run qa:critical`: PASS.
- Ruff fatal-error rules and Python bytecode compilation: PASS.
- Django system check and migration drift check: PASS.
- OpenAPI validation with warnings treated as failures: PASS.
- Backend tests: 538 passed, 34 skipped.
- Script tests: 28 passed.
- Plugin validation, build, package, immutability and dependency-lock gates: PASS.
- Official plugin `0.4.2` package identity remains unchanged from `db85e08`.
- AstrBot Bridge tests: 50 passed; validation and packaging: PASS.
- `git diff --check`: PASS.

The final Batch D PR must still pass PR CI, PR Release Gate, Squash Merge, post-merge CI and post-merge Release Gate. Exact run IDs, final main SHA and merged PR metadata are recorded in the final execution report after the merge exists.

## V1.0 Recommendation

The public contracts and dependency directions are ready to freeze for v1.0. After the final gates pass, the next stage is Architecture Hardening Production Acceptance. Production deployment remains outside this phase.
