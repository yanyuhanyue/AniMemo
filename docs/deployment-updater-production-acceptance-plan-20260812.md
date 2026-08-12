# AniMemo Deployment / Updater Production Acceptance Plan

Status: **PLAN ONLY — NOT RUN**
Prepared: 2026-08-12
Production baseline to audit before execution: `6452b3dbfff39529c49c2bc69ede1f3d76236eee`

This plan is for a later approved production acceptance window. It does not authorize SSH, Agent installation, release creation, database migration, R2 write, OpenResty/Cloudflare change or any other production mutation during the hardening phase.

## Preconditions

Stop unless all are true:

1. final candidate is merged to `main` and the exact merge SHA has passing PR Fast, exact-SHA Pre-Merge Full, main lightweight, full CI and Release Gate evidence;
2. a real immutable RC exists, was produced by the manual Release Producer, and has passing read-only/attestation verification;
3. the audited production SHA, Compose project, data roots, PostgreSQL/Redis container IDs, API/Web identities and shared VPS inventory are recorded;
4. maintenance window, operator, rollback owner and communication channel are explicit;
5. a fresh database backup can be created and independently verified before migration/switch;
6. the target Manifest declares the observed live database/configuration/Plugin SDK contracts compatible.

Any identity mismatch, unverified attestation, missing backup, unavailable health signal, unexpected migration, shared-service ambiguity or minimum-Updater mismatch is a stop condition.

## Read-only baseline evidence

Record without mutation:

```text
production Git SHA
legacy releases/current.json and archive SHA
Compose project and config
PostgreSQL/Redis/API/Web container IDs, images and restart counts
database migration plan and contract
enabled Plugin SDK APIs and official plugin health
Integration/Bridge health and pending receipt diagnostics
media backend configuration, usage and reservation counts
verified backup inventory
disk/memory capacity
OpenResty route boundary and localhost Web port
unrelated VPS container/service inventory
```

Do not print secrets or use real user credentials for smoke. If a dedicated smoke identity does not exist, authenticated smoke is `NOT RUN`, not improvised with an administrator account.

## First cutover sequence

1. Verify the RC Manifest, checksums and all GitHub attestations against the exact repositories, workflows and source commits.
2. Pull the exact API/Web digests. Do not deploy mutable tags.
3. Create a fresh `pg_dump` gzip under `/data/anime-journal/backups`; verify metadata, compressed SHA-256, timestamp and full gzip decompression.
4. Install the reviewed Agent package with `deploy/install-updater.sh`; verify only Agent-owned files/service changed.
5. Verify systemd status, journal, Updater version, socket path/mode/group and absence of any TCP listener.
6. Perform the one-time legacy-to-digest cutover using the exact RC images and explicit migration/bootstrap jobs. Replace only API/Web; preserve PostgreSQL/Redis container IDs and data mounts.
7. Observe the defined stable window: every observation requires API/Web healthy, restart count zero, HTTP 200 from `/health/`, `/`, `/login`, `/api/schema/`, `/api/docs/`, and no HTTP 5xx or critical/fatal/panic/Traceback in API/Web stdout or stderr since the window began.
8. Run public health, frontend, schema/docs, plugin, Integration/Bridge, media reservation and dedicated authenticated smoke checks.
9. Import the exact already-verified RC Manifest as CURRENT once. Confirm it records the actual enabled Plugin SDK APIs and rejects a repeated import.
10. Confirm Staff UI shows the same CURRENT version, commit and API/Web digests, the actual enabled Plugin SDK APIs, and no PREVIOUS unless a real Agent switch has occurred.
11. Perform one scoped API/Web restart and repeat health/data/plugin/integration/media checks. PostgreSQL/Redis and unrelated VPS services must retain identity and health.

If the RC contains a migration, acceptance must prove its plan is expected and additive/backward-compatible before execution. Migration failure stops before switch and never triggers reverse migration or automatic restore.

## Update and rollback acceptance

After the bootstrap RC is established, a later approved RC can exercise normal Agent flow:

```text
CURRENT A
→ discover and verify B
→ plan Safe Switch
→ fresh backup if migration is required
→ pull exact B digests
→ explicit migration/bootstrap
→ scoped API/Web switch
→ stable health
→ CURRENT B / PREVIOUS A
→ compatibility-checked Application Rollback A
→ database remains at B contract
```

Also prove in isolation or an approved non-production acceptance environment:

- backup failure prevents migration/switch;
- health failure automatically restores only the compatible application;
- incompatible previous app produces `manual_recovery_required` instead of database rollback;
- Unsafe Downgrade is blocked before pull/switch;
- concurrent apply/rollback is rejected by the global lock;
- Agent restart marks pre-switch work failed and post-migration/switch work manual recovery;
- repeated rollback swaps CURRENT/PREVIOUS predictably;
- PREVIOUS compatibility updates after plugin enable/disable changes and unsafe rollback controls stay disabled;
- RPC size limits and socket permissions hold.

## Shared VPS safety

The change window must continuously prove no operation touched:

```text
AstrBot, NapCat, Gotify, dailyhub, PHP
global OpenResty lifecycle
cloudflared or Cloudflare settings
Docker daemon lifecycle
unrelated Compose projects, images, volumes or networks
```

Forbidden commands include Docker/system/volume/network prune, whole-host or unrelated `compose down`, `down -v`, Docker/PostgreSQL/Redis restart, global OpenResty restart and automatic database restore.

## Media reservation acceptance

Before and after cutover record active/abandoned reservation counts, managed MediaObject counts, local/R2 backend health and a read-only orphan audit. If a dedicated test object is authorized, use an isolated key and delete only that known key. Never delete an unknown remote object because it lacks a current database row; follow the runbook in `docs/media-storage.md`.

## Promotion

Stable promotion is allowed only after exact RC production acceptance is signed off. Promotion must:

- consume the accepted RC Manifest;
- build no image;
- preserve RC application commit and API/Web digests byte-for-byte;
- create a new Stable Manifest signed by the promotion workflow;
- use previous Stable → current Stable release notes;
- refuse an existing Stable tag/release.

## Required final report

Use only `PASS`, `FAIL`, `NOT RUN`, or `NOT APPLICABLE`. Include exact timestamps/SHA/digests and evidence locations for:

```text
PRODUCTION BASELINE IDENTITY
BACKUP
AGENT INSTALL / VERSION / SOCKET
RC RELEASE / MANIFEST / ATTESTATIONS
MIGRATION PLAN / EXECUTION
API/WEB SCOPED SWITCH
POSTGRESQL RETAINED
REDIS RETAINED
HEALTH / STABLE WINDOW / RESTART COUNTS
PUBLIC / AUTHENTICATED SMOKE
PLUGIN / INTEGRATION / BRIDGE
MEDIA RESERVATION / ORPHAN AUDIT
CURRENT / PREVIOUS / VERSION HISTORY
SCOPED RESTART
SHARED VPS ISOLATION
APPLICATION ROLLBACK PLAN OR RESULT
DATABASE RESTORE
STABLE PROMOTION
```

For the 2026-08-12 hardening phase every production execution field remains **NOT RUN**.
