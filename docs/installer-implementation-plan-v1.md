# AniMemo v1.1 Installer Implementation Plan

Status: **IMPLEMENTED IN PHASE 3C — GITHUB-HOSTED QUALIFICATION EVIDENCE REQUIRED FOR RELEASE USE**

Target: AniMemo v1.1 pre-production clean break

Scope: define and record the implemented Installer for **Fresh Install** and **Restore-to-New-Instance**. Phase 3C updates the Release Producer only enough to create and verify exact Installer materials; it does not publish an installer, modify `install.animemo.cc`, create a Release, or authorize Production work.

## 1. Outcome and invariants

The Installer is a thin orchestration module over existing durability and release modules. It must not become a second compatibility evaluator, backup verifier, restore engine, migration engine, secret-envelope implementation, Doctor, instance locator, or Release Authority.

A successful installation means all of the following are true:

1. An exact release was resolved from the fixed GitHub authority and its complete trust chain was verified.
2. Only the canonical v1.1 roots were created, with the frozen ownership and permission model.
3. PostgreSQL, Redis, migration/bootstrap jobs, API, Web, Compose, and the Host Updater are installed as one AniMemo-scoped instance.
4. The effective API/Web images equal the verified `repository@sha256:digest` identities.
5. Public Origin and listen remain independent; the default listen is `127.0.0.1:8088`.
6. Application health, release identity, durable state, and the read-only Doctor gate pass.
7. `/var/lib/animemo-updater/instance.json` is the last instance-discovery publication and matches config, Compose, Updater, and systemd allowlists.
8. Fresh installation creates a new instance identity exactly once. Restore and migration preserve the source `instanceId` and all memory/resource identity.

The Installer never treats “containers are running” as success. It never trusts a mutable image tag, a directory name, an environment fallback, or `install.animemo.cc` as release or instance authority.

## 2. Runtime and contract dependencies

The implementation must call the following real interfaces rather than duplicating their rules.

| Authority module | Existing interface consumed by Installer | Installer use |
| --- | --- | --- |
| Compatibility | `durability.compatibility.evaluate_compatibility(...)`, `CompatibilityOperation`, `ArtifactIdentity`, ordered `DimensionAssessment` values | Produce the sole install/restore/migration compatibility decision. Operational collection errors remain errors and never become a fifth outcome. |
| Backup | `durability.backup.verify_backup(path)` and `BackupVerification.as_compatibility_artifact()` | Restore preflight accepts only a read-only verified, `FINALIZED` Backup Format v1 artifact. Installer does not parse the backup again. |
| Restore | `durability.restore.prepare_restore(RestoreRequest)` and `execute_restore(...)`; adapter seams `DestinationPort`, `ReleasePort`, `UpdaterPort`, `DatabasePort`, `MutationPort`, `ValidationPort`, and secret resolvers | Own all Backup verification, compatibility planning/reverification, logical PostgreSQL restore, protected secret resolution, filesystem staging, validation, publication, and `RECOVERY_REQUIRED` evidence. Installer assembles concrete adapters and accepts the exact plan; it does not duplicate the state machine. |
| Migration | `durability.migration.create_migration_bundle(...)`, `verify_migration_bundle(...)`, `consume_migration_bundle(...)`, and `authorize_activation(...)`; target seam `MigrationTargetWriter` | Migration remains a separate instance-movement operation. A concrete target writer may compose Installer bootstrap primitives, but Installer does not reinterpret a migration bundle as a backup, duplicate envelope/target staging rules, or add a hidden install mode. |
| Secret Envelope | `durability.secret_envelope.open_secret_envelope(...)` through Restore/Migration Runtime | Wrong key or tamper fails before destructive mutation. Installer never decrypts or re-encrypts bundle secrets on its own and never generates a replacement CEK for restored ciphertext. |
| Doctor | `durability.doctor.DoctorRunner.run()` and `DoctorReport` | Final read-only acceptance. Production probe adapters must cover every required check; `SKIPPED` is not install success. |
| Filesystem/Locator | Canonical constants, `parse_instance_locator(...)`, and `load_instance_locator(...)` in `durability.instance` | One canonical path vocabulary, state classification, same-instance verification, and post-publication discovery. No env/cwd/legacy fallback. |
| Release consumer | `updater.source.GitHubReleaseSource.list_releases(...)` and `fetch_verified(..., refresh=True)`, backed by `release.contract` validation | Discover eligible candidates and reverify the exact selected tag, Manifest, deployment contract, checksums, tag peel, attestations, provenance, and API/Web digests. |
| Updater | Existing exact-release verification, immutable Compose deployment, operation/slot semantics, and one-time CURRENT adoption | Installer hands an installed instance to Updater after initial exact identity is proven. It never writes CURRENT/PREVIOUS by hand. |

These runtime interfaces are the integration seams. Their presence is not permission to add parallel parsers or domain orchestration inside the Installer.

## 3. Mandatory pre-implementation gates

All five gates were closed or narrowly re-frozen before the Full Installer was declared implemented. A real GitHub-hosted qualification run remains required evidence before any Release use.

### 3.1 Satisfied baseline: PCDC-001 canonical clean break

The Coordinator completed and re-froze PCDC-001 before runtime implementation. The governing contracts and the implemented locator now accept only:

```text
/opt/animemo
/data/animemo
/opt/animemo-updater
/var/lib/animemo-updater
/run/animemo-updater
deploymentProfile = v1.1-standard
```

The correction, affected documents/tests, integrity impact, and zero-debt conclusion are recorded in [v1.1 Pre-Production Debt Ledger](v1.1-pre-production-debt-ledger.md#pcdc-001--canonical-filesystem-clean-break). This gate is satisfied. The Installer supports only those canonical paths; pre-v1.1 layouts are neither auto-detected nor adapted. This plan contains no legacy branch and no custom-root branch.

### 3.2 PASS: complete installer artifact profile

Release Manifest/deployment schema v2 now binds deterministic `installer-materials.tar`, every regular member's canonical path, mode, size and SHA-256, the archive digest, and the complete offline material set required by Installer and Updater. `release.materials` performs bounded exact inspection/extraction and rejects missing, duplicate, unordered, unexpected, linked, oversized or changed material.

The Release trust chain remains GitHub Release plus exact GHCR digests, Manifest, checksums, deployment/material contract, provenance and attestation. PostgreSQL and Redis use frozen `docker.io/library/...@sha256:` identities; mutable dependency tags are rejected. Platform qualification binds those same dependency digests. This is a narrow Release Contract schema-v2 correction, recorded in the Phase 3C correction document; promotion policy is unchanged.

### 3.3 PASS: canonical Updater discovery and adoption

Updater now consumes only the strict canonical locator, managed configuration and fixed roots. `adopt_initial_release(...)` accepts a protected fixed request, re-verifies exact release/config/material/locator bindings, serializes through the operation lock, establishes native state once, and publishes the locator through CAS as the final discovery publication. Installer and Restore call this interface; they never hand-edit CURRENT/PREVIOUS. Interrupted adoption and locator publication become durable manual recovery and block later updates until reconciliation.

### 3.4 PASS: managed configuration and Compose contract

`/data/animemo/config/animemo.json` (`animemo.managed-config/v1`) is the only configuration authority. It has a strict schema, explicit secret classification, private single-link regular-file checks and same-directory atomic replacement. `/run/animemo-updater/managed.env` is a private, derived and rebuildable Compose Adapter, never a reader or second authority. The exact Compose bytes support independent listen address/port. The locator writer binds config revision, Public Origin, Listen, release identity and canonical paths; config, locator, Compose and Updater mismatches fail closed.

### 3.5 PASS PENDING HOSTED EVIDENCE: platform qualification

`animemo.platform-qualification/v1` defines `v1.1-standard-linux-amd64` through measured capabilities rather than invented distribution/version floors. The GitHub-hosted qualification job proves Docker/Compose behavior, systemd, secure filesystem semantics, exact PostgreSQL/Redis images, PostgreSQL 16 logical dump/restore, disk/memory/port and user/group operations, then emits candidate/workflow/run-bound evidence. Installer Compatibility consumes this exact evidence and cannot return compatible for a different profile or candidate. Local contract/rehearsal tests pass; a real hosted run must produce the final evidence before Release use.

### 3.6 PASS: durable partial-install evidence

Fresh Install uses the independent `animemo.operation` schema-v1 journal with stable phases, timestamps, completed steps, exact instance/release binding, error code and recovery status. Before irreversible mutation it performs only ownership-scoped cleanup or rollback. After database migration begins, failure cannot delete data or reverse migration and becomes `MANUAL_RECOVERY_REQUIRED`. Interrupted Restore uses a separate `restore-operations/` journal and blocks Updater lifecycle work until explicit reconciliation. Neither journal stores secrets.

## 4. Planned module shape

The external seam is deliberately small:

```text
plan(request, read_only_host) -> InstallPlan
execute(accepted_plan, mutation_adapters) -> InstallResult
```

`InstallPlan` binds the normalized inputs, mode, exact target release identity, compatibility decision, target-state classification, listen/Public Origin, required mutations, warnings, and a digest of every evaluated input. `execute` refuses a stale or mismatched plan and repeats exact release verification at the execution boundary.

The implementation may contain these internal modules. Their interfaces stay private to the Installer unless another runtime already owns the seam.

| Internal module | Responsibility | Required reuse |
| --- | --- | --- |
| Input normalizer | Strict channel/version, mode, origin, listen, dry-run, and non-interactive validation | Public Origin/Listen Contract; no network or mutation |
| State classifier | Classify fresh, same-version, different-version, partial, foreign, corrupt locator, port collision | `durability.instance`; Compose/systemd read-only evidence |
| Release resolver adapter | Stable/RC filtering, exact selection, full fresh verification, verified material staging | `GitHubReleaseSource` and `release.contract`; no second verifier |
| Install compatibility adapter | Collect all seven ordered dimensions and call the canonical evaluator | `evaluate_compatibility("install", ...)` |
| Canonical target preparer | Create only approved users/groups/roots/modes after authority PASS | Filesystem Layout v1 constants and qualified host adapter |
| Managed config publisher | Validate, secret-safe generate/restore, private atomic write, rollback previous complete file | Frozen config schema; same protected directory atomicity |
| Compose adapter | Materialize verified Compose bytes and exact image environment; run only AniMemo project operations | Exact release material; argv lists; no shell-generated business logic |
| Updater adoption adapter | Install exact Updater assets, establish allowlists, adopt exact CURRENT once | Approved canonical Updater interface |
| Acceptance runner | Local API/Web/release checks followed by complete Doctor report | `DoctorRunner`; no mutating probes |
| Transaction coordinator | Phase order, unique ownership evidence, safe cleanup, partial/recovery result | Durable operation model; never owns domain restore/migration logic |

Deleting the Installer orchestration module should force all ordering, stale-plan defense, and cleanup policy back into callers. If deletion only removes pass-through calls, the module is too shallow and must be redesigned before implementation.

## 5. Shared bootstrap primitives

Fresh, Restore-to-New, and future migration-target orchestration must share the following primitives rather than copy an installer:

1. strict input normalization;
2. read-only target classification;
3. platform/tool/disk/filesystem/port preflight;
4. exact release resolution and verification;
5. canonical root/user/group preparation;
6. verified deployment material staging;
7. managed config validation and atomic publication;
8. exact Compose materialization and AniMemo-scoped lifecycle;
9. Updater installation/adoption through its approved interface;
10. local health, exact running release verification, Doctor, and locator publication;
11. transaction evidence and ownership-scoped cleanup.

The orchestration differs by mode. A shared primitive never decides Backup, Restore, or Migration semantics and never accepts arbitrary paths/commands from a bundle.

## 6. Fresh Install orchestration

The planned sequence is:

```text
Parse/normalize
→ classify target
→ read-only host and port preflight
→ resolve exact release
→ verify complete release/material trust chain
→ evaluate INSTALL compatibility
→ emit dry-run result or begin mutation
→ create private operation staging
→ create canonical identities/roots
→ publish protected fresh config and secrets
→ install verified Compose/Updater material
→ start PostgreSQL and Redis
→ run explicit migration job
→ run explicit bootstrap job
→ start API and Web
→ local health + exact effective release checks
→ adopt exact CURRENT through Updater
→ atomically publish instance.json
→ complete read-only Doctor gate
→ commit operation success
```

Fresh-specific rules:

- Generate one new UUID `instanceId` once and bind it to the accepted plan and operation record.
- Generate database credentials, `DJANGO_SECRET_KEY`, and `CREDENTIAL_ENCRYPTION_KEY` with the platform CSPRNG; write them directly to the protected managed config with mode `0600`. Never print, log, place in argv, store in locator, or stage in the application tree.
- Derive `ALLOWED_HOSTS`, CORS, CSRF, and provider callback identity from the validated Public Origin. Never derive listen from Public Origin.
- `migration` and `bootstrap` are explicit one-shot Compose jobs. A service start must not hide them.
- Bootstrap provisions the existing private first-run setup lifecycle. Installation success does not create the administrator account and must not expose the setup code in general logs.
- The same-version path is a verified no-op only after locator, release digests, Compose, Updater, config, and health all agree. It never reruns migration/bootstrap or rotates secrets.
- A different installed version is an Updater handoff error, not an install/upgrade path.

## 7. Restore-to-New-Instance orchestration

Restore-to-New accepts a Backup Format v1 artifact; it is not a reinstall shortcut and is not migration.

```text
Parse/normalize restore request
→ assemble concrete RestoreRequest ports over shared primitives
→ prepare_restore(request)
   (Backup VERIFY + Fresh/Existing Empty classification + secret authentication
    + Release/Updater evidence + canonical compatibility plan)
→ display and accept the exact RestorePlan.plan_digest
→ emit dry-run result or execute_restore(request, plan, accepted_plan_digest=...)
→ runtime reverifies the entire plan and reacquires exact release material
→ runtime drives inactive target/database/filesystem/config/Updater staging
→ runtime runs its complete ValidationPort gate
→ runtime publishes PUBLISHED or records RECOVERY_REQUIRED
→ on PUBLISHED, run final complete Doctor acceptance
```

Restore-specific rules:

- The Installer never directly extracts a member, invokes `pg_restore`, copies protected files, or parses a Secret Envelope. The concrete adapters use `SubprocessPostgresRestore`, `EnvelopeSecretResolver`/`ReferenceSecretResolver`/`NoneSecretResolver`, and the approved mutation/validation implementations behind the Restore Runtime seams.
- Only `FINALIZED` artifacts accepted by `verify_backup` proceed. Staging, partial, checksum-invalid, claimed-v1 malformed, or blindly unknown artifacts fail closed.
- `instanceId`, `CREDENTIAL_ENCRYPTION_KEY`, database/config contracts, and stable memory/resource references are preserved. The Installer must not generate a replacement CEK.
- Target-local database/Redis credentials may be newly generated only when the accepted Restore Plan explicitly classifies them as target-local; this does not authorize rotating restored application secrets.
- Existing Active, Foreign, Partial, Ambiguous, or data-bearing-without-locator targets are rejected. There is no overwrite/adopt/repair flag.
- The concrete `ReleasePort` and `UpdaterPort` must reverify exact authority and approved adoption evidence during both planning and execution. Restore publication cannot hand-edit Updater state. If exact release adoption is unavailable, return the stable contract blocker rather than publish an apparently healthy instance.
- If a failure happens after database mutation, preserve the inactive target and Restore Runtime evidence. Do not delete database/media/plugins, reverse migrations, or claim idempotent success.

## 8. Migration integration seam

Migration is invoked through the Phase 3B migration command surface (`migration create`, `migration verify`, `migration consume`), not through an Installer `--migration` alias. Those commands remain thin adapters over `create_migration_bundle`, `verify_migration_bundle`, and `consume_migration_bundle`.

`migration consume` receives a concrete `MigrationTargetWriter` whose implementation may compose the shared read-only preflight, exact release acquisition, canonical target preparation, Compose, Updater adoption, health, Doctor, and locator publication primitives. `consume_migration_bundle` remains the owner of bundle reverification, Secret Envelope authentication, config disposition, plugin/CAS/local-media/database staging, inactive validation/publication, and rollback calls. `authorize_activation` remains the pure owner of source→target handoff authorization; Installer never activates or deletes the source.

The Installer primitive layer must never:

- copy source absolute paths into target config;
- generate a new `instanceId` for a migration;
- infer same-R2 from a similar name;
- activate a target while the source is writable;
- delete or retire the source;
- turn an unsupported transfer or plugin into a partial install.

Source retirement is explicit and remains outside Installer scope. Successful target activation does not authorize source deletion.

## 9. Release resolution and trust chain

`install.animemo.cc` transports only the bootstrap program. It does not select a release and is not a Manifest, checksum, provenance, attestation, or image authority. Until bootstrap bytes have their own independently verifiable provenance path, documentation must state that executing the downloaded bootstrap still trusts that transport endpoint.

The future bootstrap download flow is: download over HTTPS to a newly created private temporary file; reject redirects or an unexpected content type/size; optionally display the byte digest; and, once Section 3.2 defines the profile, offer an operator-verifiable checksum/provenance path obtained independently from the fixed GitHub repository before `sudo` execution. The downloaded bootstrap then resolves and verifies the application release again from the fixed authority. An internal Manifest check cannot retroactively make already-executing bootstrap bytes trustworthy, so the implementation must not advertise end-to-end bootstrap provenance before that independent path exists.

Resolution rules:

- `--channel stable` selects the highest strict `vMAJOR.MINOR.PATCH` candidate with `draft=false` and `prerelease=false`.
- `--channel rc` selects the highest strict `vMAJOR.MINOR.PATCH-rc.N` candidate with `draft=false` and `prerelease=true`; Stable and Beta are not candidates.
- `--version TAG` accepts only an exact Stable or RC tag and performs no channel resolution.
- `--channel` and `--version` are mutually exclusive. “latest”, ranges, branches, commits, mutable OCI tags, and partial versions are rejected.
- Failure verifying the selected highest candidate is terminal; do not silently fall back to an older release.

Before any persistent mutation, and again at execution, the release adapter must prove:

1. exact GitHub Release metadata and strict tag/channel agreement;
2. fixed three-asset set and exact checksums;
3. Manifest schema, version, channel, `release.commit`, `provenance.sourceCommit`, minimum Updater, and compatibility identities;
4. bounded tag peel to the exact release commit;
5. deployment contract canonical digest and file identities;
6. fixed API/Web repositories, linux/amd64 platform, and exact digests;
7. OCI and file attestations bound to subject, workflow, repository, OIDC issuer, main ref, source commit, and SLSA predicate;
8. every executed installer/deployment/updater byte under the future complete artifact profile;
9. actual running API/Web identities equal the verified digests.

A dry-run verification result or cache is not execution authority. Credentials may improve availability for the fixed authority only and must never alter repository/URL/image identity.

## 10. Filesystem, locator, config, and secrets

Only these roots are valid:

| Root | Purpose |
| --- | --- |
| `/opt/animemo` | verified, replaceable, non-secret application/deployment material |
| `/data/animemo` | durable database, protected config, plugins, media, private state, backups, logs |
| `/opt/animemo-updater` | verified Updater program material |
| `/var/lib/animemo-updater` | durable Updater/operation/locator state |
| `/run/animemo-updater` | ephemeral socket/runtime state |

The preparer must use `lstat`-style checks for every ancestor/target, reject link/reparse/hard-link/path-escape cases, enforce non-overlap, and refuse foreign or unknown contents. It must not recursively `chown` an existing tree to make classification pass.

Protected config lives under `/data/animemo/config/`, not `/opt/animemo`. Secret-bearing files are private and atomically replaced in the same directory with file and parent fsync. Fresh secrets, restored secrets, and target-local secrets have distinct provenance in the accepted plan. Plaintext secret values never enter plan JSON, reports, operation journals, process argv, or locator.

The final locator uses schema v1 and binds exactly:

- preserved/new `instanceId` according to mode;
- canonical roots and `v1.1-standard` profile;
- canonical `{host, port}` listen identity;
- canonical Public Origin;
- the protected managed-config path;
- non-secret exact release identity.

It is written as an `animemo-updater` owned, `0600`, single-link regular file using a private same-directory temporary file, validation, fsync, and atomic replace. Publication happens only after config, Compose, Updater, systemd allowlist, and running release evidence agree. `load_instance_locator()` must be able to read back the exact published object; no fallback reconstructs it.

## 11. PostgreSQL, Redis, Compose, and application jobs

- Standard Compose owns the instance-scoped PostgreSQL and Redis containers; the administrator owns the Docker daemon and host resources.
- Fresh install creates an empty PostgreSQL data root and uses explicit logical application migrations.
- Restore/Migration use only the approved logical PostgreSQL dump/restore path. They never copy or replace live PGDATA.
- Redis is operational/recreatable state, not a formal Backup/Restore database member. The Installer does not import a source Redis tree.
- Compose operations are fixed argv operations against verified files and project `animemo`; no arbitrary service/project/file/command input crosses the installer interface.
- API/Web images always use exact verified `repository@sha256:digest`. Pulling or starting by mutable tag is forbidden.
- Installer never restarts Docker, prunes global resources, stops foreign containers, or touches another Compose project/network/volume.
- Migration and bootstrap are explicit jobs with captured stable results. Business logic does not live in shell/argument handlers.

## 12. Public Origin and listen

The default is exactly:

```text
127.0.0.1:8088
```

Any other loopback port/address must be explicit. A collision reports the requested endpoint and fails; Installer does not kill the owner, choose a random port, change firewall rules, or fall back to a wildcard bind.

`0.0.0.0` is allowed only when the operator explicitly supplies it through `--listen`; no default, inference, or fallback may select it. Interactive and non-interactive execution both emit the warning before mutation, and the warning remains present in machine/human output. The warning covers lack of automatic HTTPS, Secure Cookie behavior, OAuth/provider callbacks, Turnstile, firewall, and public exposure responsibility.

Public Origin is an explicit canonical `http`/`https` origin without userinfo/path/query/fragment. Interactive mode prompts for it. Non-interactive mode requires explicit `ANIMEMO_PUBLIC_ORIGIN`; missing/empty/invalid input fails. Installer never guesses it from server IP, listen, Host headers, DNS, or a proxy.

DNS, TLS, reverse proxy, provider callback registration, and public reachability are administrator-owned. They are not install success gates and are not configured by AniMemo.

## 13. Preflight, dry-run, and non-interactive behavior

Read-only preflight covers arguments, EUID, Linux/amd64 qualification, systemd, Docker daemon, Compose v2, HTTPS/attestation tooling, fixed-authority network access, disk space, filesystem type/safety, root overlap, locator, foreign/partial contents, Compose identity, port bindability, Public Origin, and systemd allowlist consistency.

`--dry-run` executes parsing, state classification, host capability collection, exact release verification, compatibility evaluation, warning generation, and the prospective operation plan. It may use a private bounded temporary verification directory and must remove it. It does not create roots/config/locator, pull images, change the Docker store, create/start/stop containers, install/reload/enable systemd units, run jobs, call Restore mutation, or write durable state. A condition that would fail real execution returns the same stable error class.

`--non-interactive` prohibits stdin/TTY prompts and implicit acceptance. Every required operator value is explicit; missing values fail. It never weakens collision, secret, wildcard-listen, foreign-state, or destructive protection. Secret input for Restore/Migration must use the runtime's approved protected descriptor/file interface when frozen; a passphrase in argv or general environment is not acceptable.

## 14. Idempotency and state classification

| Observed state | Result |
| --- | --- |
| Canonical roots absent/empty, locator absent, no Compose/port collision | Fresh execution may proceed after all gates. |
| Valid matching locator and exact same running release/config/Compose/Updater, all health gates PASS | No-op success; no secret rotation, migration, bootstrap, or rewrite. |
| Valid matching instance at a different release | Fail and hand off to Updater; Installer does not upgrade or downgrade. |
| Valid locator but requested mode/root/origin/listen conflicts | `instance_conflict`; no mutation. |
| Corrupt locator | Fail closed; do not rebuild from env/directories. |
| Partial app/data/updater/systemd/Compose state | `partial_installation`; no resume/repair guess. |
| Data without a valid locator | Fail closed; do not adopt, initialize, restore over, or reset. |
| Foreign files/Compose/network/volume/container/port | Fail closed; do not move, overwrite, stop, or delete. |
| Restore target is Existing Active, Foreign, Partial, or Ambiguous | Reject before destructive mutation. |

Execution rechecks state and release identity immediately before first mutation. Plan digests prevent a plan built for one target/artifact/release from being replayed against another.

## 15. Failure cleanup and recovery

Before full release verification, only private ephemeral verification material may exist. After verification, every staging path carries a unique operation identity and a manifest of objects created by that operation.

Automatic cleanup may remove only uncommitted staging objects created by the current operation and still proven to be owned by it. Cleanup never removes an execution-preexisting path, finalized backup/bundle, database, media, plugin CAS/data, protected config, Updater history, or source instance. It never performs reverse database migrations.

Failure phases:

- Before mutation: stable error, `mutationOccurred=false`, zero persistent change.
- Before database/config publication with safely reversible staging: stop scoped services if created this run, remove owned staging, preserve preexisting state.
- After database import/migration or another irreversible change: keep target inactive, retain redacted operation evidence, return `manual_recovery_required` or Restore `RECOVERY_REQUIRED`.
- After locator publication: failure is not silently converted to success; record the exact failed acceptance gate and require explicit recovery. Locator removal is permitted only if the transaction can prove it created the locator and removal cannot hide durable state; otherwise preserve evidence and fail closed.

All output includes stable phase/error class, non-secret path/endpoint/release identity, whether mutation occurred, and the safe next action. Raw exception text and secret values are never control or output data.

## 16. Health and Doctor acceptance

Pre-publication local acceptance requires:

- PostgreSQL usable and expected database contract present;
- Redis reachable with no source-state import assumption;
- application schema/migration state accepted by the exact release;
- API `/health/` PASS with exact release identity;
- Web and required local paths return expected responses;
- API/Web containers healthy with no restart/critical-log failure during a stable observation window;
- protected config and restored credential decryptability;
- instance identity and critical memory/data presence for Restore/Migration;
- Updater service, state, allowlist, and exact release adoption aligned.

After atomic locator publication, run a complete `DoctorRunner` with concrete, strictly read-only probe adapters. Every required check must be present; an unavailable adapter yields `SKIPPED` and blocks install success. Doctor reports local application health separately from administrator-owned DNS/TLS/proxy reachability.

Restore/Migration acceptance additionally carries MI-1 through MI-5 evidence: external metadata loss does not remove memory; provider identity changes do not orphan stable relations; history/merge references remain; unsupported future payload is not discarded; ambiguity fails closed.

## 17. Upgrade handoff

Installer owns only initial installation and exact initial adoption. After success:

- Updater owns release discovery, execution-time re-verification, compatibility, backup gate, explicit migration/bootstrap, API/Web switch, health observation, and CURRENT/PREVIOUS history.
- Installer invoked against a different release returns a stable “use Updater” result and performs zero mutation.
- Public Origin or listen changes are configuration transactions, not upgrades and not hidden Installer reruns.
- Installer does not expose `animemo update`, rollback, config, domain, or listen management commands.

The handoff is complete only when Updater reads the same locator/config/roots and verifies the same exact running release. Until Section 3.3 is resolved, this handoff is blocked.

## 18. Uninstall boundary

Uninstall is not implemented by this plan and is never an install failure cleanup strategy. A future separately contracted uninstall may remove verified, inactive, AniMemo-owned replaceable application material, service units, launcher, and ephemeral runtime state after exact locator/ownership checks.

By default it must preserve `/data/animemo`, protected config/secrets, PostgreSQL data, plugins, media, private state, backups, Updater history, and recovery evidence. Destructive data purge requires a separate explicit contract and exact confirmation; `--non-interactive`, “reinstall”, health failure, or directory existence is never deletion consent. Foreign and ambiguous objects always block deletion.

## 19. Stable command/output surface

The Full Installer may expose only the narrow install surface already frozen:

```text
install [--channel stable|rc | --version TAG]
        [--listen ADDRESS:PORT]
        [--dry-run]
        [--non-interactive]

restore-to-new --backup PATH
               [--channel stable|rc | --version TAG when accepted by Restore Plan]
               [--listen ADDRESS:PORT]
               [--dry-run]
               [--non-interactive]
```

Exact secret-input flags remain blocked until the Secret Envelope runtime's safe invocation contract is frozen. The bootstrap shell is a transport/launcher only; argument parsing and business rules live in the tested Installer domain module.

Stable error classes include `usage_error`, `unsupported_platform`, `dependency_unavailable`, `release_unavailable`, `release_verification_failed`, `compatibility_rejected`, `filesystem_conflict`, `instance_conflict`, `port_conflict`, `configuration_invalid`, `backup_verification_failed`, `restore_rejected`, `partial_installation`, `health_check_failed`, and `manual_recovery_required`. Runtime-owned stable codes remain nested evidence rather than being rewritten into misleading installer outcomes.

This plan does not create a full management CLI. Restore and migration domain interfaces remain callable through their narrow Phase 3B entry points; no business logic is placed in `argparse`/shell handlers.

## 20. Implementation decomposition after the gates pass

### Workstream A — freeze interfaces

- Keep the completed PCDC-001 clean break as the fixed baseline; complete the remaining Release/Updater/config/partial-state reviews in Section 3.
- Pin the implemented Restore/Migration symbols and plan/result identities in integration tests.
- Freeze locator writer, managed config schema, qualified platform profile, and complete Doctor probes.

### Workstream B — pure planning path

- Implement strict input types and canonicalization.
- Implement read-only state/host evidence adapters.
- Adapt the existing Release Consumer to return a complete verified release/material identity.
- Build ordered install compatibility assessments and canonical `InstallPlan` digest.
- Implement deterministic human/JSON dry-run rendering and redaction tests.

### Workstream C — shared mutation primitives

- Canonical root/user/group creation with ownership-scoped rollback.
- Protected config/secret generation and atomic publisher.
- Verified material staging, exact Compose adapter, and updater adoption adapter.
- Durable Installer transaction record and recovery handoff.
- Complete read-only health/Doctor acceptance adapter.

### Workstream D — mode orchestration

- Fresh orchestration over shared primitives.
- Restore-to-New orchestration over the real Restore Runtime.
- Migration target reuse through the real Migration Runtime without adding an Installer mode.

### Workstream E — contract and integration tests

- Trust-chain tamper, stale plan/cache, highest-candidate failure, exact digest, and unbound-byte rejection.
- Fresh, same-version no-op, different-version Updater handoff, partial/foreign/data-without-locator rejection.
- Default/alternate loopback, collision, explicit `0.0.0.0` warning, origin/listen independence.
- Dry-run zero mutation and non-interactive zero prompt.
- Config/secret redaction, private modes, link/path traversal, atomic locator/config failure.
- PostgreSQL/Redis/Compose job ordering and no global Docker/foreign workload mutation.
- Restore finalized-only, CEK/instanceId preservation, wrong key/tamper before mutation, logical DB path, recovery evidence, and MI-1..MI-5.
- Doctor complete-probe acceptance and Updater canonical handoff.
- Cleanup deletion-safety fault injection at every mutation boundary.

## 21. Explicit exclusions

The Installer implementation and this plan exclude:

- 1Panel, 宝塔, aaPanel, or any hosting-panel integration;
- Nginx, OpenResty, Caddy, Traefik, or other proxy management;
- Cloudflare, tunnel, DNS, TLS, certificate, certbot, firewall, or public 80/443 automation;
- legacy root detection, old config/env aliases, old bundle readers, `/data/anime-journal`, source-layout migration, dual paths, or compatibility shims;
- automatic source deletion/retirement after migration;
- automatic repair, adoption, reset, data purge, random port selection, or foreign process/container handling;
- mutable image tags, alternate repositories/registries/URLs, or installer-domain release authority;
- management surface beyond `animemo-updater config` show/validate/dry-run/set/apply, update/rollback CLI, or uninstall implementation;
- Release Producer changes beyond the exact Installer Material Profile, installer artifact publication, `install.animemo.cc` changes, RC/Stable Release, or Production deployment;
- database schema or Resource Identity changes. If Installer work appears to require them, stop with `DATABASE_CONTRACT_REVIEW_REQUIRED`.

## 22. Plan acceptance

### Phase 3C re-freeze and implementation

Before implementation, the five mandatory gaps in Section 3 are corrected by
the narrow pre-production record in
[phase3c-pre-production-contract-corrections.md](phase3c-pre-production-contract-corrections.md):

| Gate | Re-frozen contract | Implementation boundary |
| --- | --- | --- |
| A | Release Manifest/material schema v2, four exact assets, deterministic uncompressed material archive, four exact image identities | `release.materials` and `VerifiedReleaseMaterials` only |
| B | Canonical `/opt/animemo` discovery, strict locator, initial adoption, locator-last publication | `durability.instance` and Updater adoption Interface |
| C | `/data/animemo/config/animemo.json`, strict secret-safe schema, ephemeral Compose env Adapter, atomic config transaction | managed-config Module and fixed Compose Adapter |
| D | `animemo.platform-qualification/v1` evidence and `v1.1-standard-linux-amd64` profile | capability collector/evaluator; no invented floors |
| E | `animemo.operation/v1` envelope, explicit irreversible boundary, global recovery barrier | Fresh Installer operation Module |

These are clean-break prerequisite corrections, not compatibility Adapters.
Their scoped contract tests passed before the Full Installer completion claim.
Fresh and Restore-to-New now use the exact Restore/Migration interfaces,
secret-free plans, canonical Updater handoff and complete Doctor acceptance.
Dry-run and non-interactive tests prove no prompt or persistent mutation before
accepted execution, and no legacy compatibility branch exists.

Current implementation result:

```text
INSTALLER_IMPLEMENTATION_PLAN=IMPLEMENTED
FULL_INSTALLER_IMPLEMENTATION=LOCAL_PASS
GITHUB_HOSTED_PLATFORM_EVIDENCE=REQUIRED_BEFORE_RELEASE
```

This result does not authorize an RC, Stable Release or Production operation.
