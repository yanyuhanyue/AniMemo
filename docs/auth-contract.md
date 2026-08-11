# AniMemo Authentication Contract

Baseline: `bbff1354f235a180a48c3f216b94c8b295f1cd96`
Contract date: `2026-08-11`

## Contract Status

AniMemo authentication separates reusable session and token semantics from Web-only transport behavior. The current Web product keeps its established security model: access tokens live only in JavaScript memory, refresh credentials live only in an HttpOnly cookie, and cookie-backed mutations require CSRF protection. This phase does not change login, refresh rotation, logout, 2FA, staff authorization or account-revocation behavior.

The canonical client routes are under `/api/v1/`. Existing `/api/` routes remain compatibility aliases backed by the same Views and services as documented in `docs/api-v1-contract.md`.

## Frontend Boundary

| Module | Contract responsibility | Forbidden dependency |
| --- | --- | --- |
| `src/lib/apiCore.js` | API v1 paths, auth endpoint names, error normalization, challenge normalization | DOM, browser storage, cookies, transport library |
| `src/lib/authSession.js` | In-memory access token and authenticated-user snapshot/subscription | DOM, browser storage, cookies, HTTP transport |
| `src/lib/webApiTransport.js` | Axios instances, same-origin credentials, Bearer injection, one-retry 401 hook, server-state invalidation | Auth product policy or persistent token storage |
| `src/lib/webAuthAdapter.js` | CSRF acquisition, refresh sharing, login/logout flows, legacy browser-token scrubbing | Domain state or UI widget behavior |
| `src/lib/api.js` | Web composition facade and compatibility exports | New auth/session business logic |

The facade preserves the existing imports used by the Web application and official frontend plugins. No refresh credential is returned from `getStoredTokens`, written to `localStorage` or written to `sessionStorage`.

## Backend Boundary

| Module | Contract responsibility |
| --- | --- |
| `backend/journal/auth_tokens.py` | Token issuance, validation, rotation, replay defense, revocation and session-version semantics |
| `backend/journal/web_auth_adapter.py` | Refresh-cookie attributes, cookie set/clear, no-store response headers, request IP and challenge extraction |
| `backend/journal/anti_abuse.py` | Provider-neutral challenge value, provider adapter lookup and fail-closed verification |
| `backend/journal/turnstile.py` | Cloudflare Turnstile provider verification using only a token and remote IP |
| `backend/journal/auth_views.py` | HTTP endpoint orchestration, serializers, throttles, audit and auth-service calls |

Token core must not write cookies or construct HTTP responses. Provider verification must not receive a Django or DRF request object.

## Web Session Lifecycle

1. Login and staff login obtain a CSRF token, submit credentials with the CSRF header, receive an access token response and an HttpOnly refresh cookie, then rotate the CSRF token.
2. The caller stores the access token and user claims in `authSession`; refresh material never enters the session object.
3. Authenticated requests add `Authorization: Bearer <access>` in the Web transport.
4. A non-auth request that receives 401 may trigger one shared refresh operation. Concurrent failures join the same promise and each original request is retried at most once.
5. Refresh uses the HttpOnly cookie plus CSRF, rotates the refresh credential on the server and replaces the in-memory access token.
6. Logout sends the current access token and cookie-backed CSRF request before clearing memory and cached CSRF state. Server-side refresh/access revocation semantics remain authoritative.
7. Initialization refreshes the session, then merges `/auth/me/` profile data without discarding staff or role claims returned by refresh.

Auth infrastructure requests themselves are never recursively refreshed after a 401.

## Anti-Abuse Challenge

The canonical request field is provider-neutral:

```json
{
  "challenge": {
    "provider": "turnstile",
    "token": "provider-proof"
  }
}
```

During the v1.0 compatibility window, Web requests using Turnstile also send the deprecated alias:

```json
{
  "cf-turnstile-response": "provider-proof"
}
```

Rules:

- If `challenge` is present, it is authoritative. An invalid canonical value fails closed and cannot fall back to the legacy alias.
- If `challenge` is absent, the legacy Turnstile field remains accepted.
- Unknown providers fail closed.
- The current stable failure code remains `turnstile_failed`; clients must not branch on the translated detail string.
- The Turnstile widget remains a Web UI adapter. Auth core does not depend on its DOM API.

The challenge requirement continues to apply to ordinary login, staff login, registration request/completion and password reset request/confirmation according to the existing endpoint policy.

## Compatibility Invariants

- HttpOnly refresh cookie: unchanged.
- Secure, SameSite, domain and path cookie settings: unchanged and settings-driven.
- CSRF enforcement and post-login rotation: unchanged.
- Refresh rotation, replay rejection and session-version revocation: unchanged.
- Access-token memory-only policy: unchanged.
- TOTP, one-time recovery codes and staff second-factor session: unchanged.
- Staff capability and account-deletion checks: unchanged.
- Dashboard and authentication UI behavior: unchanged.
- Legacy `/api/` auth aliases: retained; new clients use `/api/v1/`.

## Future Mobile Adapter

Mobile implementation is deferred. A future adapter may reuse API paths, challenge/error normalization and auth response semantics without importing the Web transport. Its access token remains in memory; its refresh credential must use iOS Keychain, Android Keystore or an equivalent secure store and must not use AsyncStorage. Bearer transport and a mobile-compatible challenge provider can be added without changing token core or Web cookie behavior.

## Enforcement

- Frontend boundary tests import `apiCore` and `authSession` in a non-browser Node environment.
- Runtime tests cover shared refresh, claim merging, challenge payload compatibility, CSRF rotation and authenticated logout cleanup.
- Backend boundary tests assert that token core exports no cookie/HTTP helpers and that provider verification receives only provider data.
- Security regression covers login, refresh, logout, invalid/expired/replayed credentials, CSRF, password changes, registration, 2FA, recovery codes, staff sessions and revocation.
- OpenAPI publishes canonical `challenge` schemas and marks `cf-turnstile-response` deprecated.

## Deferred

- Mobile authentication implementation: deferred.
- New anti-abuse providers or app attestation: deferred.
- Removal of the legacy Turnstile field: deferred to a separately announced compatibility decision.
- Auth product redesign or persistent Web tokens: not applicable.
- Database migration: not applicable.
