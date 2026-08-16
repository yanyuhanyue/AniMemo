# AniMemo v1.1 Phase 3C Pre-Production Contract Corrections

Status: RE-FROZEN AND IMPLEMENTED IN PHASE 3C

Scope: the following corrections are limited to the unpublished v1.1 standard
deployment profile. They do not change database, Resource Identity, Memory
Identity, Backup Format v1, Migration Bundle v1, Secret Envelope v1, or the
fundamental exact-release trust model.

## P3C-GA — complete exact release materials

### CURRENT CONTRACT

Release Manifest schema v1 binds the API and Web image digests and publishes
three assets: `release-manifest.json`, `deployment-contract.json`, and
`checksums.txt`. The deployment contract identifies two Compose files, but the
Release does not transport them. PostgreSQL and Redis use mutable tags and the
Updater consumer returns a manifest after deleting its temporary download.

### CURRENT PROBLEM

An Installer cannot reconstruct every executed byte from Release authority.
Installer, Updater, Compose, systemd, launcher, configuration schema, and Python
dependency bytes are not one durable verified set. PostgreSQL and Redis can
change while the Release Manifest remains unchanged.

### WHY IT CREATES LONG-TERM DEBT

Allowing each consumer to fetch source files, resolve Python packages, or pull
mutable dependency tags creates several release authorities and makes exact
installation, promotion, rollback, and incident reconstruction impossible.

### WHY OLD BEHAVIOR IS NOT A REQUIRED COMPATIBILITY CONTRACT

AniMemo v1.1 is pre-production. No schema-v1 Release has been installed under a
supported v1.1 Installer contract. Producer and consumer may move together
without a dual reader; an old RC must not be promoted.

### PROPOSED CLEAN CONTRACT

Release Manifest schema v2 publishes exactly four assets:

1. `release-manifest.json`
2. `deployment-contract.json`
3. `installer-materials.tar`
4. `checksums.txt`

The tar is deterministic and uncompressed. The deployment contract binds every
member by canonical relative path, SHA-256, size, mode, and semantic role. It
also binds `v1.1-standard`, `linux/amd64`, the archive digest, and four exact
image references. PostgreSQL is fixed to
`docker.io/library/postgres@sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571`
and Redis to
`docker.io/library/redis@sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf`
for the qualified `linux/amd64` baseline. The bundle contains an offline Python
wheelhouse; install-time package resolution is forbidden. Stable promotion
copies the exact RC bundle and contracts and never rebuilds them.

`VerifiedReleaseMaterials` is the only Installer/Updater Interface. Verification
rejects missing/extra assets, links, special files, absolute or parent paths,
duplicates, size/count excess, digest mismatch, and any uncontracted member.

### DATA IMPACT

No database or durable instance data enters the bundle. PostgreSQL remains on
the approved major-16 baseline; PGDATA is never copied or replaced.

### MEMORY INTEGRITY IMPACT

None. Instance identity, CEK, Resource Identity, provider bindings, and MI-1
through MI-5 are unchanged.

### RELEASE IMPACT

Manifest and material schemas advance explicitly to v2. Beta/RC production,
consumer validation, rehearsal, and Stable promotion change together. A new RC
is required; no Release or tag is created by Phase 3C.

### MIGRATION IMPACT

Restore and Migration obtain program bytes through the same verified-material
Interface. Their artifact formats and logical PostgreSQL path are unchanged.

### ROLLBACK / RECOVERY IMPACT

Application rollback remains an API/Web slot switch against the same qualified
datastore baseline. A datastore digest transition fails closed and requires a
separate future contract. No source checkout, tag, cache, or online resolver is
a fallback.

## P3C-GB — canonical instance discovery and Updater adoption

### CURRENT CONTRACT

The locator parser knows the five canonical roots, while the active Updater
deployment still assumes a 1Panel root and `.env.production`. Initial import
validates a local manifest but does not freshly verify GitHub Release authority
or the running release. There is no secure canonical locator writer.

### CURRENT PROBLEM

Installer and Updater do not share one discovery Seam. Initial import can
partially mutate Updater state without an adoption operation barrier, and the
locator reader/writer boundary does not prove same-inode secure I/O.

### WHY IT CREATES LONG-TERM DEBT

Two roots, two configuration authorities, or guessed adoption state would make
every update and recovery path ambiguous and would weaken CURRENT/PREVIOUS and
exact-release guarantees.

### WHY OLD BEHAVIOR IS NOT A REQUIRED COMPATIBILITY CONTRACT

1Panel, old `.env.production`, legacy slot files, custom roots, environment,
and cwd discovery were never a supported v1.1 production contract. Pre-release
instances receive no automatic adoption or compatibility Adapter.

### PROPOSED CLEAN CONTRACT

`CanonicalInstanceRegistry.snapshot()` securely reads exactly
`/var/lib/animemo-updater/instance.json`. The schema is closed and canonical;
the managed config path is exactly `/data/animemo/config/animemo.json` and the
locator carries its non-secret revision. Same-inode reads and same-directory
atomic no-clobber/CAS writes enforce regular-file, single-link, owner, and mode
rules.

`adopt_initial_release(request) -> AdoptionReceipt` is the only initial
adoption Interface. It freshly verifies exact Release materials and running
identity under the global operation lock, initializes native Updater state via
`ReleaseSlots`, and publishes the locator last. Any failure after durable
mutation enters `manual_recovery_required`. Ordinary second adoption is
rejected; recovery is an explicit operation-id-bound host action.

### DATA IMPACT

No legacy data is copied or deleted. Existing unexpected state is Foreign or
Partial/Ambiguous and blocks mutation.

### MEMORY INTEGRITY IMPACT

Fresh creates one new instance ID; Restore and Migration preserve the source
ID. Locator and adoption evidence are secret-free.

### RELEASE IMPACT

Compose, Updater, systemd, and locator schema bytes are part of the v2 material
profile. Adoption always refreshes Release verification.

### MIGRATION IMPACT

Migration target code calls the adoption Interface and never writes CURRENT,
PREVIOUS, runtime state, or locator directly.

### ROLLBACK / RECOVERY IMPACT

Slot history remains Updater-owned. Failed adoption is durable and blocks all
lifecycle operations until explicit reconciliation; no file deletion or legacy
state guess clears the barrier.

## P3C-GC — one managed configuration authority

### CURRENT CONTRACT

The public-origin/listen contract separates external identity from the local
bind and requires a protected configuration root, but no exact filename,
closed schema, atomic writer, or management transaction exists. Compose reads
an app-tree `.env.production` and accepts root/port fallback variables.

### CURRENT PROBLEM

Secrets, Public Origin, listen, Compose input, locator mirrors, and application
derived settings can diverge. Directly making `.env` canonical would also bind
the domain model to Compose parsing and quoting semantics.

### WHY IT CREATES LONG-TERM DEBT

Dual files and implicit environment precedence make configuration updates,
rollback, secret redaction, Restore, and exact diagnosis non-deterministic.

### WHY OLD BEHAVIOR IS NOT A REQUIRED COMPATIBILITY CONTRACT

No v1.1 Stable installation exists. App-tree env files, legacy aliases, custom
roots, and arbitrary environment passthrough are removed without readers,
writers, or migration shims.

### PROPOSED CLEAN CONTRACT

The sole authority is `/data/animemo/config/animemo.json`, identity
`animemo.managed-config/v1`. UTF-8 canonical JSON uses a closed schema and
bounded strings. It contains canonical instance/config identities, Public
Origin, listen, and an explicit allowlist of application/database/provider
fields. Unknown fields or versions fail closed. Secret values are held only in
the private `0600` file and secret-bearing in-memory objects.

The managed-config Module derives application values and a private ephemeral
Compose env at `/run/animemo-updater/managed.env`. This file is `0600`, written
atomically, tied to the config revision and exact Release, and deleted/rebuilt
as runtime state; it is never read as configuration authority. The Compose
Adapter uses a sanitized process environment and fixed argv/files/services.

Management uses `plan/change/apply`: bind the current revision and instance ID,
validate, persist a durable operation, atomically replace config, reconcile
AniMemo API/Web, probe local health and exact release, update the locator mirror
with CAS, run relevant Doctor checks, then commit. Failure restores the private
rollback shadow or enters `manual_recovery_required`. Show, diff, journal, and
errors expose only non-secret fields and `configured|missing|invalid` status.

### DATA IMPACT

Managed config is durable data and is not overwritten by a Release update.
Only operation-owned rollback shadows may be removed after success.

### MEMORY INTEGRITY IMPACT

Domain/listen mutation does not change CEK, Django key, database credentials,
instance ID, memory, media, plugins, or provider history.

### RELEASE IMPACT

The schema/parser, runtime env Adapter, Compose template, and management runtime
are exact bound material. Release identity is not stored as config authority.

### MIGRATION IMPACT

The Migration target writer creates this one schema using preserved,
reconfigured, and target-local dispositions. It does not copy a source absolute
path or create a second env authority.

### ROLLBACK / RECOVERY IMPACT

Config and locator revisions roll back together with scoped API/Web reconcile.
Rollback never changes PostgreSQL/Redis data, Docker daemon, proxy, DNS, TLS, or
firewall. Failed rollback leaves durable recovery evidence.

## P3C-GD — qualification-backed platform support

### CURRENT CONTRACT

Compatibility includes a platform dimension, but current CI runs on the moving
`ubuntu-latest` label and records neither a qualified host tuple nor actual
Docker, Compose, systemd, filesystem, and PostgreSQL logical-transfer facts.

### CURRENT PROBLEM

The Installer cannot truthfully return COMPATIBLE from `Linux/amd64` alone, and
invented version floors would have no evidence connection to features used.

### WHY IT CREATES LONG-TERM DEBT

Unproven floors turn host compatibility into guesswork and permit installation
to cross irreversible boundaries on unsupported environments.

### WHY OLD BEHAVIOR IS NOT A REQUIRED COMPATIBILITY CONTRACT

No broad Linux support promise has shipped. The first supported profile may be
narrow and capability-backed without preserving an `ubuntu-latest` guess.

### PROPOSED CLEAN CONTRACT

Qualification identity is `animemo.platform-qualification/v1`, profile
`v1.1-standard-linux-amd64`. A signed/checksummed evidence document binds exact
candidate SHA, workflow/run, observed OS/kernel/architecture, systemd, Docker
Engine and Compose v2 capabilities, POSIX secure-file semantics, disk/memory,
ports, exact dependency images, PostgreSQL 16 source/`pg_dump`/`psql`/target
tuple, Fresh/Restore/Migration rehearsals, and complete Doctor acceptance.

`collect_host_capabilities`, `verify_platform_qualification`, and
`assess_platform` form the Interface. Missing collection is an evaluation
error; collected unsupported capability is UNSUPPORTED. Version floors are
added only after recorded qualification evidence proves them.

### DATA IMPACT

Collection and assessment are read-only. They do not initialize roots,
containers, databases, users, or services.

### MEMORY INTEGRITY IMPACT

Qualification rehearsals assert MI-1 through MI-5 using representative rows
and opaque bytes; the profile itself contains no instance data.

### RELEASE IMPACT

Evidence binds an exact candidate and dependency digests. Moving runner labels
or skipped jobs are not authority.

### MIGRATION IMPACT

The qualified PostgreSQL path is plain logical dump plus `psql
--single-transaction`, matching the implemented runtime. `pg_restore` is not a
v1 requirement.

### ROLLBACK / RECOVERY IMPACT

Qualification failure occurs before mutation and requires no rollback.

## P3C-GE — one durable lifecycle operation envelope

### CURRENT CONTRACT

Updater operations are durable but update-specific, unversioned, and permit
arbitrary metadata. Restore has separate recovery evidence. Fresh install has
no durable phase record or explicit irreversible boundary.

### CURRENT PROBLEM

A crash around configuration publication, database migration, bootstrap,
Updater adoption, locator publication, or Doctor cannot be classified safely.

### WHY IT CREATES LONG-TERM DEBT

Ad-hoc marker files and per-feature journals would duplicate recovery state and
allow one lifecycle action to ignore another action's recovery barrier.

### WHY OLD BEHAVIOR IS NOT A REQUIRED COMPATIBILITY CONTRACT

Unversioned pre-v1.1 records are not imported. Unknown schema/kind/state fails
closed and is never guessed, deleted, or silently rewritten.

### PROPOSED CLEAN CONTRACT

Fresh Install and Updater use strict, versioned operation records under
`/var/lib/animemo-updater/operations/`. `fresh_install` records non-secret plan,
instance, release/material, config-revision, phase, ordered completed steps,
mutation and irreversible flags, stable error code, target-active status, and
bounded stable-code events. Restore retains its frozen domain-specific evidence
under the separate `/var/lib/animemo-updater/restore-operations/` namespace so
neither schema becomes an optional-field union. Updater recovery discovery
checks both namespaces and enforces one lifecycle barrier. Arbitrary
metadata/detail is forbidden.

Fresh phases are preflight, roots, config, services, database migration,
bootstrap, runtime, validation, adoption/locator, Doctor, and succeeded. The
irreversible flag is fsynced before database migration. A crash after that flag
never reruns migration and becomes `manual_recovery_required`. The global
recovery barrier covers every operation kind.

### DATA IMPACT

Before the irreversible boundary, cleanup removes only operation-owned staging.
After it, PostgreSQL, config, media, plugins, memory, and history are preserved
and the target remains inactive until explicit recovery.

### MEMORY INTEGRITY IMPACT

Records contain no secret, database row, config value/fingerprint, memory
payload, or provider credential. Recovery never deletes opaque user data.

### RELEASE IMPACT

Operation schema/runtime is included in exact Installer materials. Evidence
binds exact Release/material identity without becoming Release authority.

### MIGRATION IMPACT

Restore/Migration keep their frozen domain evidence while participating in the
same global lifecycle recovery barrier; their artifact schemas do not change
and are not stored as Updater operation envelopes.

### ROLLBACK / RECOVERY IMPACT

Terminal states are succeeded, failed-without-mutation, rolled-back,
manual-recovery-required, and reconciled. Reconcile is explicit, operation-ID
bound, host-only, and revalidates actual state before changing the barrier.

## Decision

These five corrections are narrow, pre-production, clean-break prerequisites.
No compatibility Adapter or dual Interface is authorized. Phase 3C implements
all five contracts. Local contract/rehearsal suites pass; final platform PASS
still requires the candidate-bound GitHub-hosted qualification evidence before
Release use.
