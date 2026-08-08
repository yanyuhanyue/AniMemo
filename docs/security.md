# Security Baseline

Refresh tokens remain HttpOnly and rotated; access tokens stay in memory;
CSRF, session-version revocation, TOTP/recovery codes, staff fail-closed
authorization and plugin package validation are enabled by default. Do not
commit `.env`, SQLite databases, media, static build output or plugin staging
directories.
