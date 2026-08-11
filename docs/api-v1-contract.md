# AniMemo API v1 Contract

Baseline: `bbff1354f235a180a48c3f216b94c8b295f1cd96`
Contract date: `2026-08-11`

## Contract Status

`/api/v1/` is the canonical AniMemo Core client contract. Existing `/api/` Core routes are compatibility aliases backed by the same Django URL patterns, Views, Serializers, permissions and domain implementation. They are not a second API and must not receive legacy-only endpoints.

`/api/integrations/v1/` remains the independently frozen Integration Protocol v1. `/health/`, `/api/schema/` and `/api/docs/` are infrastructure endpoints. Dynamic `/api/v1/plugins/<slug>/...` routes are governed by the installed plugin package and Plugin SDK contract, so they are intentionally excluded from the static Core OpenAPI document.

## API Inventory

The generated `/api/schema/` document is the exhaustive method-level inventory and the machine-readable source of truth for request schemas, response schemas, query parameters, pagination, authentication and status codes. This table records ownership and client classification for every stable route family.

| Area | Canonical path(s) | Methods | Auth / permission | Contract schemas | Surface |
| --- | --- | --- | --- | --- | --- |
| Login | `/api/v1/token/` | POST | Public + CSRF + anti-abuse challenge | `TokenLoginRequest`, `LoginResponse`, `ApiError` | Web, future Mobile adapter |
| Refresh | `/api/v1/token/refresh/` | POST | Web refresh HttpOnly cookie + CSRF | no body, `AccessTokenResponse`, `ApiError` | Web adapter |
| Registration | `/api/v1/auth/register/request/`, `verify/`, `complete/` | POST | Public; request/complete require challenge | registration serializers, `ApiError` | Web, future Mobile adapter |
| Session | `/api/v1/auth/me/`, `logout/`, `password-change/`, `account/` | GET, POST, DELETE | Bearer; logout additionally accepts refresh cookie | auth request/response serializers, `ApiError` | Web, future Mobile adapter |
| Password reset | `/api/v1/auth/password-reset/`, `password-reset-confirm/` | POST | Public + anti-abuse challenge | reset serializers, `ApiError` | Web, future Mobile adapter |
| Web CSRF | `/api/v1/auth/csrf/` | GET | Public | `CsrfTokenResponse` | Web adapter only |
| Staff login | `/api/v1/auth/staff-login/` | POST | Public + CSRF + challenge + staff/2FA policy | login serializers, `ApiError` | Staff Web |
| Entries | `/api/v1/entries/`, `/api/v1/entries/{id}/` | GET, POST, PUT, PATCH, DELETE | Bearer + owner isolation | `JournalEntrySerializer`, paginated response, `ApiError` | Web, future Mobile, Plugin via Host capability |
| External identities | `/api/v1/entries/{id}/external-identities/...` | GET, POST, DELETE | Bearer + entry owner | external identity DTOs, `ApiError` | Web, future Mobile |
| Watch history | `/api/v1/entries/{entry_id}/watch-history/`, `.../{record_id}/` | GET, POST, PUT, PATCH, DELETE | Bearer + entry owner | watch-history serializers, paginated response, `ApiError` | Web, future Mobile, Plugin via Host capability |
| Filters | `/api/v1/filters/`, `/api/v1/filters/{id}/` | GET, POST, PUT, PATCH, DELETE | Bearer + owner isolation | `QuickFilterSerializer`, `ApiError` | Web, future Mobile |
| Profile/settings | `/api/v1/settings/me/`, `/api/v1/public-journal/status/` | GET, POST, PATCH | Bearer + current user | profile serializers, `ApiError` | Web, future Mobile |
| Analytics | `/api/v1/stats/me/` | GET | Bearer + current user | analytics DTO, date query parameters, `ApiError` | Web, future Mobile, Plugin read capability |
| Import/export | `/api/v1/import/`, `/api/v1/export/` | POST, GET | Bearer + current user | Data Bundle v1 / file responses, `ApiError` | Web; Mobile deferred |
| Columns | `/api/v1/columns/`, `/api/v1/columns/{id}/`, submit/removal actions | GET, POST, PUT, PATCH, DELETE | Bearer + author/workflow permission | column serializers, `ApiError` | Web; Mobile optional |
| Public discovery | `/api/v1/homepage/`, `featured/`, `showcases/`, `showcase/{public_slug}/`, `shared/{share_slug}/`, `site-settings/`, `tag-presets/` | GET | Public or optional Bearer | public DTO serializers, `ApiError` | Web, future Mobile |
| Public catalog | `/api/v1/catalog/public-search/` | GET | Bearer | paginated catalog DTO, query parameters, `ApiError` | Web, future Mobile |
| External media | `/api/v1/external-media/providers/{provider}/...` | GET | Public or Bearer depending on operation | provider-neutral media DTOs, `ApiError` | Web, future Mobile |
| External accounts | `/api/v1/external-accounts/...` | GET, POST, DELETE | Bearer except provider callback | provider capability/connection/import DTOs, `ApiError` | Web, future Mobile adapter |
| External sync | `/api/v1/external-sync/providers/{provider}/entries/{entry_id}/preview/`, `apply/` | GET, POST | Bearer + owner isolation | preview/apply serializers, `ApiError` | Web, future Mobile |
| Plugin platform | `/api/v1/plugins/enabled/`, `installed/`, `marketplace/...`, `my/...`, `policy/`, `previews/...` | GET, POST, PATCH, DELETE | Public metadata or Bearer owner/developer policy | plugin platform DTOs, `ApiError` | Web, frontend Plugin runtime bootstrap |
| Plugin runtime | `/api/v1/plugins/{slug}/{plugin_path}` | Manifest-declared | Manifest access + installation + actor binding | plugin-owned; outside Core OpenAPI | Plugin |
| Staff | `/api/v1/staff/...` | GET, POST, PATCH, DELETE | Bearer + explicit Staff capability; sensitive actions may require reauthentication/2FA | staff DTOs, `ApiError` | Staff Web only |
| Integration v1 | `/api/integrations/v1/...` | GET, POST, DELETE | Bearer for pairing/admin; HMAC for actions/events | Integration Protocol v1 schemas, `ApiError` | Integration, AstrBot Bridge |

## Error Contract

All JSON errors use the canonical shape:

```json
{
  "code": "validation_error",
  "detail": "请求参数无效。",
  "fields": { "title": ["该字段不能为空。"] }
}
```

`code` and HTTP status are machine contracts. `detail` is human-readable and may be reworded. `fields` and non-sensitive `metadata` are optional. Every OpenAPI operation references `ApiError` for applicable common statuses and for its default error response. Frontend behavior must branch on `code`, never translated text.

## Pagination And Query Stability

Page-number pagination remains `{count, next, previous, results}`. Entry queries retain `page`, `page_size`, `search`, `ordering`, `watch_status`, `tags`, `priority`, `activity` and Quick Filter semantics already covered by Dashboard regression tests. A future cursor contract requires a new documented version or an additive endpoint; it cannot silently replace v1 page semantics.

## Stable Resource Identity

| Resource | Stable identity | Notes |
| --- | --- | --- |
| User, JournalEntry, WatchHistory, QuickFilter | integer ID | Stable database identity; never list position or mutable display text |
| Public journal / shared entry / Column public link | UUID slug | Public identifier, independent from username/title |
| External account | owner + provider; provider external user ID | Display name and username are not identity |
| External import preview | UUID | Expiring workflow identity |
| Plugin | immutable `plugin_id`, unique slug, immutable `slug + version` release identity | Package blob SHA and canonical content digest remain separate identities |
| Integration connection | UUID | Provider instance and key ID are constrained identities |
| Integration mutation | connection + `request_id` | Idempotency identity |
| MediaObject | UUID + storage backend/object key | Upload keys use immutable user ID + random UUID, not username/title |

No identity migration is required. Owner isolation remains mandatory even for UUID identifiers.

## Legacy Compatibility

- Existing `/api/...` Core paths remain callable during the v1.0 compatibility window.
- Canonical and legacy paths are mounted from one route table and have automated callback-parity coverage.
- The Web build and frontend Plugin SDK now use `/api/v1/` by default.
- OAuth redirect URIs already registered with a legacy callback remain valid because the callback alias is retained.
- New Core client endpoints must be added to the shared v1 route table. A legacy-only `/api/foo/` route is prohibited.
- Removal timing for legacy aliases is a Final RC or later compatibility decision and is not part of this phase.

## Enforcement

- Django contract tests resolve every Core v1 OpenAPI path and its legacy alias to the same callback.
- OpenAPI generation publishes only Core v1 and Integration v1, validates without warnings, and attaches `ApiError` references.
- Stable identity tests freeze UUID/public-slug, plugin release, Integration idempotency and media-key invariants.
- Frontend security tests assert the same-origin `/api/v1` default; critical browser regressions exercise the canonical path.

## Deferred

- Generated Mobile client: deferred.
- Legacy route removal: deferred.
- Dynamic plugin schema aggregation: deferred to a separately versioned Plugin contract artifact.
- API v2: not applicable.
