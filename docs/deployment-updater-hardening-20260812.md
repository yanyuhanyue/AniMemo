# AniMemo v1.0 Deployment / Updater Hardening Report

Date: 2026-08-12
Scope: implementation, CI validation, read-only release dry-run design, isolated updater validation, documentation and future production migration plan.
Production deploy: **NOT RUN**.

## Current Deployment Architecture (before this phase)

```text
GitHub source / CI / Release Gate
→ core-only ZIP + SHA file
→ production VPS
→ deploy/deploy.sh
→ server-side API/Web build
→ whole Compose stop/start
  ├ PostgreSQL
  ├ Redis
  ├ API
  └ Web
→ smoke
→ releases/current.json archive identity
```

The historical full release path validated the ZIP, copied the server environment, prepared fixed data roots, built API/Web on the VPS, replaced the application tree, started the full stack, ran smoke, optionally reloaded the AniMemo OpenResty site configuration, and archived `current.json`. Scoped hotfix runbooks separately built and replaced API/Web while preserving PostgreSQL/Redis. Stateful CI started a production baseline whose API entrypoint implicitly ran migration/bootstrap/static work before Gunicorn. Backup and smoke existed, but release identity was a source archive plus server-local image build rather than immutable OCI digest.

## Target Architecture

```text
GitHub main
→ CI / authoritative Release Gate
→ manual Release Producer
→ Beta or RC
→ build immutable artifacts once
  ├ ghcr.io/yanyuhanyue/animemo-api@sha256
  ├ ghcr.io/yanyuhanyue/animemo-web@sha256
  ├ release-manifest.json
  ├ checksums.txt
  └ GitHub/SLSA attestations
→ GitHub Pre-release + GHCR
→ restricted Host Update Agent
→ preflight / compatibility / backup / pull / migration / bootstrap
→ scoped API/Web switch / stable health window
→ future production acceptance
→ promote exact RC artifacts
→ Stable GitHub Release
```

The governing rule is **BUILD ONCE, PROMOTE MANY**. Stable promotion never builds API or Web again. Production Compose is digest-only; CI/fresh/legacy bootstrap adds `deploy/docker-compose.build.yml` instead of maintaining a second full Compose definition.

## Release channels and identity

Beta and RC are GitHub Pre-releases in `yanyuhanyue/AniMemo`; Stable is a GitHub Release promoted from an exact RC. Tags are `vMAJOR.MINOR.PATCH-beta.N`, `-rc.N`, and Stable `vMAJOR.MINOR.PATCH`. The first release line has a strict bootstrap override; later versions derive from the latest Stable. Existing tags/releases are never overwritten.

Application identity is VERSION + CHANNEL + 40-character COMMIT + API/Web repository/digest. Mutable tags may exist for human tooling, but the Release Consumer deploys only `repository@sha256:digest`. Runtime labels expose version/commit/channel without making Git SHA the human version.

## Manifest and provenance

Schema v1 declares release identity, exact OCI subjects, minimum updater version, explicit database/configuration/Plugin SDK compatibility, migration policy, application rollback policy, release notes identity and provenance.

Prerelease API/Web and Manifest attestations are created by `.github/workflows/release.yml`. Stable preserves the RC application commit and OCI digests; its new Manifest is signed by `.github/workflows/promote-release.yml`. Therefore:

```text
release.commit              = application/build commit
provenance.sourceCommit     = commit that ran the signing workflow
```

The Agent verifies each subject with the correct source commit. No custom crypto or committed long-lived signing key was introduced.

## Compose and migration contract

`deploy/docker-compose.yml` has no API/Web `build:` keys. It contains explicit one-shot `migration` and `bootstrap` services and ordinary Gunicorn-only API startup. `deploy/docker-compose.build.yml` adds build definitions for CI/fresh/legacy scenarios.

Fresh Gate order:

```text
build api/web
→ start postgres/redis
→ migration
→ bootstrap
→ start api/web
→ health and contract checks
```

Stateful Gate starts the audited Base with its historical behavior, records PostgreSQL/Redis container identities, builds Current through the current build override, runs Current migration/bootstrap explicitly, replaces only API, and proves PostgreSQL/Redis containers were retained.

`deploy/deploy.sh` remains only as explicit `--bootstrap` or `--break-glass`; it is not a normal update command. It uses the build override, explicit jobs and scoped API/Web replacement. It never automatically reverses a migration or restores the database.

## Update Agent threat model

The Agent has powerful host authority because Docker access is effectively privileged. The safety model is therefore narrow interface plus fixed resources, not a claim of perfect sandboxing:

- independent systemd Host Service;
- local AF_UNIX socket only, mode/group constrained;
- strict operation and parameter allowlist;
- fixed GitHub/GHCR identities, paths, project and service names;
- no arbitrary shell, URL, repository, image, path, container or service;
- atomic state, immutable history, crash-aware global lock and startup recovery;
- bounded RPC and redacted logs;
- Django receives only the socket, never Docker authority.

Full contract: `docs/update-agent-v1.md`.

## Compatibility, migration and rollback

Safe Switch is computed from live database/configuration contracts, enabled Plugin SDK APIs and target app acceptance. The model is explicit application compatibility metadata, not migration filename comparison.

- no migration: recent verified backup required; automatic app rollback can be safe;
- additive backward-compatible migration: fresh backup, explicit migration, possible app rollback with database retained;
- breaking/incompatible contract: switch blocked as Unsafe Downgrade.

Database reverse migration and automatic database restore are absent by design.

## Staff UX and API

The real Staff system surface shows CURRENT/PREVIOUS identities, channel selection, compatibility, exact confirmation, migration/rollback facts, persistent Operation progress and history. Stable is the default; RC/Beta require superuser, with Beta marked experimental. Mutation endpoints require staff capability, CSRF, throttle and audit records. Both apply and rollback return a durable background Operation rather than keeping an HTTP request open for host work.

## Bootstrap and cutover

Existing ZIP archives and `releases/current.json` are retained as legacy evidence. They are not silently converted into signed OCI identity. Future first cutover must build/publish a verified RC for the audited production baseline, create a fresh backup, install the Agent, switch API/Web to that exact digest without replacing PostgreSQL/Redis, verify health/smoke, and only then import the matching CURRENT Manifest once.

The detailed future procedure and stop conditions are in `docs/deployment-updater-production-acceptance-plan-20260812.md`. This phase did not perform it.

## Verification status

Implemented local evidence includes release contract/CLI tests, updater security/state/compatibility/executor tests, deployment contract tests, frontend tests/lint/build and Staff API tests. Windows lacks usable AF_UNIX and Docker in this workspace, so Unix Socket and real Compose execution are not claimed from local results. `Release Gate / updater-isolated` runs the complete updater suite on Ubuntu, where Socket permissions/limits and the A → B → health → Application Rollback A scenario execute rather than skip. Fresh Docker and production-baseline stateful upgrade remain authoritative Ubuntu jobs.

Production VPS, production database, R2, Cloudflare, OpenResty, Docker daemon and shared VPS services were not changed.
