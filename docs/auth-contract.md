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

The same Web adapter owns the additive first-run status/setup calls. It submits the one-time code through the existing CSRF cookie flow and never stores that code in browser storage. First-run state and administrator creation are defined separately in `docs/first-run-bootstrap.md`; they do not change normal login, refresh or token semantics.

## Backend Boundary

| Module | Contract responsibility |
| --- | --- |
| `backend/journal/auth_tokens.py` | Token issuance, validation, rotation, replay defense, revocation and session-version semantics |
| `backend/journal/web_auth_adapter.py` | Refresh-cookie attributes, cookie set/clear, no-store response headers, request IP, Bearer credential and challenge extraction |
| `backend/journal/anti_abuse.py` | Provider-neutral challenge value, provider adapter lookup and fail-closed verification |
| `backend/journal/turnstile.py` | Cloudflare Turnstile provider verification using only a token and remote IP |
| `backend/journal/auth_views.py` | HTTP endpoint orchestration, serializers, throttles, audit and auth-service calls |
| `backend/site_config/first_run.py` | One-time code file safety, installation-state transaction and first-admin creation |

Token core accepts raw token credentials and must not read Django/DRF requests, write cookies or construct HTTP responses. Refresh replay auditing remains in the Web endpoint orchestration. Provider verification must not receive a Django or DRF request object.

## Web Session Lifecycle

1. Login and staff login obtain a CSRF token, submit credentials with the CSRF header, receive an access token response and an HttpOnly refresh cookie, then rotate the CSRF token.
2. The caller stores the access token and user claims in `authSession`; refresh material never enters the session object.
3. Authenticated requests add `Authorization: Bearer <access>` in the Web transport.
4. A non-auth request that receives 401 may trigger one shared refresh operation within its session generation. Concurrent failures join the same promise and each original request is retried at most once.
5. Refresh uses the HttpOnly cookie plus CSRF, rotates the refresh credential on the server and replaces the in-memory access token.
6. Logout sends the current access token and cookie-backed CSRF request before clearing memory and cached CSRF state. Server-side refresh/access revocation semantics remain authoritative.
7. Initialization refreshes the session, then merges `/auth/me/` profile data without discarding staff or role claims returned by refresh.

Login, staff login, logout and replacement through `storeTokens` establish a new session generation. Committing a login result establishes the new identity and invalidates requests started while that login was pending. Refresh and profile updates stay within their captured generation. Success, failure and final cleanup from an earlier generation cannot write or clear the current session, replace its CSRF cache, discard its in-flight request, or retry an old API call using a different identity. Login responses remain bound to their generation until the caller stores them; a stale `storeTokens` returns `false` and its caller stops navigation. While a login is pending or its result awaits storage, competing refresh and cookie-changing auth operations are rejected with `AUTH_SESSION_CHANGED`. A failed login releases and clears only its own generation, since cookies may already have changed before a response body or the follow-up CSRF request fails.

The transport binds the generation and Bearer credential synchronously when the API method is called, before Axios can yield to a microtask or another account can be stored. Its request interceptor is synchronous. The transport rejects stale success and error responses with `AUTH_SESSION_CHANGED`, without exposing an old 401/403 as a current-user authentication failure. Initialization shares both refresh and profile work for repeated calls in one generation, including React StrictMode. When the authenticated user ID changes, the private page and plugin state are recreated so prior personal data cannot remain visible. Page logout handlers rely on the adapter's scoped cleanup and do not clear a later session again.

Generation changes synchronously notify the Web transport so it aborts pending requests on both the Bearer and cookie clients before a new login can run. This prevents late refresh/login/logout/CSRF responses, and password/account mutation responses, from applying old `Set-Cookie` headers after the new session's cookies. Headers already processed before a transition are superseded by the new login; the old response body remains unable to update memory. Request cleanup removes caller-signal listeners, keeps caller cancellation working, and does not cancel requests from the new generation. Same-generation refresh/profile updates keep active requests intact.

The session marks a pending identity change until its login result is stored or its own generation fails. During this transition the Bearer transport rejects every request before dispatch, including staff two-factor operations that rotate cookies outside the auth facade. The login flow retains access to its own cookie client and CSRF requests.

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
- Backend boundary tests assert that token core accepts raw credentials instead of HTTP requests, exports no cookie/HTTP helpers, preserves refresh replay auditing in the Web flow, and passes only provider data to challenge verification.
- Security regression covers login, refresh, logout, invalid/expired/replayed credentials, CSRF, password changes, registration, 2FA, recovery codes, staff sessions and revocation.
- OpenAPI publishes canonical `challenge` schemas and marks `cf-turnstile-response` deprecated.

## Deferred

- Mobile authentication implementation: deferred.
- New anti-abuse providers or app attestation: deferred.
- Removal of the legacy Turnstile field: deferred to a separately announced compatibility decision.
- Auth product redesign or persistent Web tokens: not applicable.
- Database migration: not applicable.
