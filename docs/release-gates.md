# Release Gates

AniMemo uses complementary CI gates. They prevent unsafe releases earlier, but
they do not replace production runtime validation.

## Personal Repository Mode

The current `yanyuhanyue/AniMemo` repository does **not** have GitHub Merge
Queue enabled. The authoritative merge flow is therefore:

```text
PR Fast Gate
-> Pre-Merge Full Gate
-> Squash Merge
-> main Lightweight Verify
```

`merge_group` remains enabled in the CI workflow definitions as future-ready
support for a later GitHub Organization / Merge Queue migration. It is not the
active merge authority in Personal Repository Mode. A push to `main` is also
not authoritative full-regression evidence; it runs lightweight post-merge
verification only.

The protected branch should require these stable aggregate contexts with strict
base freshness and administrator enforcement:

- `pr-fast-gate`
- `pre-merge-authority`

Subsystem jobs such as `frontend`, `backend`, `postgres`, and `plugins` are
selective implementation details of PR Fast and must not be individual required
contexts. A legitimate docs-only or single-subsystem PR skips jobs outside its
risk classification.

## Gate levels

1. **Development:** targeted local tests for the change being implemented.
2. **PR Fast Gate:** changed-file classification, fast-fail checks, affected
   subsystem gates, and signal-selected release checks.
3. **Pre-Merge Full Gate:** one full regression plus Release Gate for the exact
   final PR head against current `main`.
4. **main:** lightweight post-merge verification only.
5. **RC:** the full RC Release Gate, build-once artifacts, and immutable
   promotion described by the release contract.

## Risk classification and execution policy

`scripts/ci_classify.py` emits the versioned `animemo.ci-risk/v2` document. Its
four independent dimensions are:

- `risk.level`: inherent change severity (`LOW`, `STANDARD`, `HIGH`, or
  `CRITICAL`);
- `execution.profile`: `DOCS_ONLY`, `CONTRACT_VALIDATION_ONLY`, `TARGETED`, or
  `FULL_AUTHORITY`;
- `signals`: affected components such as backend, database, updater,
  deployment, recovery, and CI authority;
- `execution.force_full`: an explicit authority decision, never an inference
  from HIGH or CRITICAL risk.

| Execution profile | Meaning | PR behavior |
| --- | --- | --- |
| `DOCS_ONLY` | Every path is audited LOW documentation | Documentation authority only |
| `CONTRACT_VALIDATION_ONLY` | Every path is in the audited Phase 2 contract set | Contract consistency, classifier self-test, compile check, and `git diff --check` |
| `TARGETED` | Product/runtime/authority changes | Signals select the minimum sufficient product and release jobs |
| `FULL_AUTHORITY` | Explicit full authority | Complete product matrix plus Updater, Docker, Stateful Upgrade, and DR Rehearsal |

HIGH and CRITICAL continue to describe important and security-sensitive changes,
but do not automatically select the whole product and release matrices. An
explicit `force_full: true` execution (Pre-Merge, Release Producer, or audited
manual dispatch), and the future `merge_group` authority event, select
`FULL_AUTHORITY` without rewriting the inherent risk. A reusable or manual run
without explicit force fails closed in the selection authority.

Empty change sets and unknown paths become CRITICAL `TARGETED` classifications
with every product, Updater, Docker, Stateful, and DR signal gate selected.
They can never become a lightweight profile. Changed-file discovery disables
rename pairing and evaluates both delete/add paths, so a move out of a sensitive
area cannot hide its original authority boundary.

### Audited contract-only profile

`CONTRACT_VALIDATION_ONLY` requires at least one of these primary documents:

- `docs/backup-contract-v1.md`
- `docs/restore-contract-v1.md`
- `docs/migration-bundle-v1.md`
- `docs/migration-secret-envelope-v1.md`
- `docs/doctor-basic-contract-v1.md`
- `docs/compatibility-matrix-v1.md`

Every changed path must also be one of those documents or an explicitly audited
support path: `CONTEXT.md`, `README.md`, `docs/data-bundle-v1.md`, or
`scripts/tests/test_recovery_migration_contracts.py`. The test is audited as a
pure contract consistency check and is explicitly excluded from recovery-runtime
filename matching. Any mixed path, suffix variation, unknown test, or actual
`durability/` runtime path exits this profile.

### Targeted signal policy

The selector uses repository behavior, not risk rank, to choose work. Important
examples are:

| Signal | Selected validation |
| --- | --- |
| frontend / backend / plugin / bridge | Owning product jobs |
| auth, API, integration, media, first-run | Backend plus relevant PostgreSQL/Bridge/runtime jobs |
| database | Backend, PostgreSQL, and Stateful Upgrade |
| updater | Bootstrap smoke, isolated Updater, and Stateful Upgrade |
| deployment | Bootstrap smoke, Fresh Docker, and Stateful Upgrade |
| recovery or migration runtime | Durability tests, PostgreSQL, and DR Rehearsal |
| CI authority | Classifier/authority scoped tests plus bootstrap smoke |

Dependency declarations add their owning component signal. Actual selection is
encoded in the canonical v2 JSON and independently rechecked by gate authority.

The classifier is only a selector. The independent `ci-selection-authority`
and `release-gate-authority` jobs parse its complete schema and the actual
`needs` results. Every selected job must be `success`, every unselected job
must be `skipped`, and event-specific lightweight jobs must match policy. A
missing output, an unexpected success, or a selected skip fails the authority
job. `pr-fast-gate` trusts only `ci-selection-authority`, not a hand-maintained
list of subsystem outcomes.

## PR Fast Gate

Ordinary PR updates use `scripts/ci_classify.py` to select the affected gates.
Docs-only, audited contract-only, frontend-only, and backend-only changes do not
automatically pay for the complete matrix. Database changes select PostgreSQL and
Stateful Upgrade; recovery runtime selects DR; deployment selects Fresh Docker;
Updater changes select the isolated Updater and stateful checks. Risk level never
silently replaces these component signals.

The stable `pr-fast-gate` aggregate succeeds only when classification and every
selected PR job succeed; unselected jobs may skip. A newer PR commit cancels the
older PR Fast run.

## Authoritative Pre-Merge Full Gate

When implementation is complete and the PR is ready to squash, dispatch
`Pre-Merge Full Gate` from `main` with:

- the PR number;
- the exact 40-character final head SHA.

The workflow runs only from the trusted current default-branch definition. Its
preflight reads the live pull request and rejects the request unless all of the
following remain true:

- the PR number is unchanged and the PR is open;
- the base branch is `main` in `yanyuhanyue/AniMemo`;
- the head repository is also `yanyuhanyue/AniMemo`;
- the live PR head equals the expected head SHA;
- the PR head contains the current `origin/main` commit.

If the branch is behind `main`, update/rebase/merge current `main` into the PR
branch using the repository's safe update mechanism, wait for the new PR Fast
result, and dispatch again with the new head SHA. The gate does not simulate
Merge Queue semantics and does not authorize a stale-base candidate.

After preflight, the workflow pins every AniMemo checkout to that exact candidate
SHA and forces:

- complete frontend, backend, bootstrap, PostgreSQL, plugin, Bridge, and runtime
  regression coverage;
- restricted Updater tests on Ubuntu, including Unix Socket/RPC limits and the
  isolated A -> B -> health -> Application Rollback A scenario;
- fresh Docker validation;
- stateful Base-to-Current upgrade validation;
- isolated A-to-B disaster-recovery rehearsal;
- the complete Release Gate.

It then reloads the PR and current `main`, repeats the PR/head/base/freshness
validation, and publishes the `pre-merge-authority` commit status on the exact
candidate SHA. Any full-gate failure or movement of the PR or base publishes a
failure. A successful status authorizes only that PR head against the base
snapshot revalidated at completion.

Commit statuses are SHA-bound. If the PR receives another commit after a pass,
the old success remains only on the old SHA and the new head has no valid
`pre-merge-authority`; run PR Fast and Pre-Merge Full again. Pre-Merge runs use a
separate per-PR, non-canceling concurrency group, so a PR Fast update cannot
cancel an authority run.

The normal operator flow is:

```bash
head_sha="$(gh pr view <pr-number> --json headRefOid --jq .headRefOid)"
gh workflow run pre-merge-full.yml --ref main \
  -f pr_number=<pr-number> \
  -f expected_head_sha="$head_sha"
```

Squash merge only after both `pr-fast-gate` and `pre-merge-authority` are green
on the current head. The same final head normally receives one authoritative
Full Regression; `main` does not repeat it.

When a PR changes CI authority itself, candidate CI cannot be its only proof.
Run this Pre-Merge workflow from the trusted old `main` definition against the
exact candidate SHA before merge. After squash merge and normal main lightweight
verification, dispatch the new main `CI` and `Release Gate` definitions against
the exact merge SHA with `force_full: true`. This old-main plus new-main pair is
the CI authority migration proof; it does not change branch-protection contexts.

## Exact-SHA reuse and build reuse

Cross-run authority reuse is **DEFERRED**. A safe reusable result would have to
bind at least the exact HEAD SHA, Base SHA, trusted workflow revision, Release
Gate revision, classifier revision, relevant test configuration, and applicable
dependency locks. A new HEAD, a moving `main`, or any change to those inputs
invalidates the result. Current GitHub job identity does not prove this complete
equivalence, so the repository continues to execute Pre-Merge Full rather than
accepting a HEAD-only cache.

Cross-job API/Web build reuse is also **DEFERRED**. Fresh Docker and stateful
upgrade currently rebuild in independent trust boundaries; moving images
between them would require immutable digest/provenance binding. Measured runs
show that the full backend suite, not image build time, is the current ordinary
full-gate critical path. The release producer remains the build-once/promote-many
authority for actual release artifacts.

The 1,500-second sustained performance baseline is not a default PR job. It is
manual/RC/risk-triggered evidence. Normal changes should complete their selected
PR gates plus one final Pre-Merge Full in tens of minutes, while component
signals retain the extended gates they actually require.

## Core CI

- `frontend`: runs JavaScript/React Hooks static checks, builds the application,
  runs frontend tests, and executes the self-contained critical browser
  regressions for authentication and Dashboard data/mutations.
- `backend`: runs Python static correctness checks, compiles Python, runs Django
  checks, verifies no missing migrations, and runs the backend test suite.
- `postgres`: runs concurrency coverage against PostgreSQL and Redis.
- `plugins`: validates/builds plugin artifacts and enforces official package
  immutability.

The official plugin gate compares the canonical package content identity from
Base with Current. It hashes a versioned, stable descriptor of the files that
actually enter the official package. A canonical content change with the same
version fails; a strict SemVer increase passes; a downgrade or unplanned official
plugin removal fails. The logs print both the canonical content digest and the
exact archive SHA for diagnostics, together with the resolved Base SHA, Head SHA,
and resolution source.

Content identity is not archive identity. The gate applies the candidate's one
canonical package builder to both Base and Current source trees, so it never
executes a package builder loaded from an arbitrary Git ref and never carries a
legacy builder. Its rebuilt archive SHA is diagnostic for that candidate
builder; it does not claim to reconstruct a historical published archive. CAS
still addresses the actual published archive bytes, and an already-published
official version retains its original blob instead of being rewritten.

## Updater background lifecycle gate

Background apply and rollback dispatches return their durable operation record
immediately, while an internal lifecycle manager retains ownership of the
mutation worker and global update lock. Operation terminal state and worker
completion are separate boundaries: completion is published only after the
executor has returned, any failure transition is durable, the lock lease is
released exactly once, and the worker is removed from the active registry.
Internal bounded `wait` and idempotent `close` barriers make test/runtime cleanup
deterministic; they are deliberately absent from the Unix RPC protocol. Runtime
shutdown rejects new mutation workers and waits for active workers without
force-killing a thread or deleting operation state.

Private state reads retain fixed-root containment, regular single-link inode,
and `O_NOFOLLOW` checks. Atomic-replacement windows (`nlink == 0`, temporary
absence/permission, or differing pre-open/open inode identity) receive bounded
retries, while symlinks, non-regular files, and hard links fail permanently.

## Fresh Docker release gate

The `docker` job validates `EMPTY DATABASE -> CURRENT RELEASE` with the base
production Compose plus `deploy/docker-compose.build.yml`. It builds API/Web,
starts PostgreSQL and Redis, waits for both healthchecks, runs the explicit target `migration` and
`bootstrap` jobs, then starts API/Web and checks health, frontend and fresh
contract state. API startup does not own migration orchestration.

## Stateful upgrade gate

The `stateful-upgrade` job validates `BASE RELEASE -> CURRENT RELEASE` without
recreating persistent state:

1. Create an isolated Compose project and runner-temporary data root.
2. Check out Base in a detached Git worktree.
3. Pull and verify Current's canonical PostgreSQL/Redis references, apply the
   current-owned upgrade override last, and render machine-readable Base and
   Current effective Compose configurations before any container starts.
4. Prove both effective configurations use the same exact dependency images,
   persistent mounts, project, and network identity; the historical Base's raw
   mutable tag literals are non-authoritative input only.
5. Build/start Base PostgreSQL, Redis, and API with `--pull never`, then record
   PostgreSQL and Redis container IDs.
6. Seed a user, journal entry, user plugin installation, `watch_history`
   `PluginData`, official project/version/blob/deployment, CAS, and runtime.
7. Build Current with the current build override.
8. Run Current's explicit `migration` and `bootstrap` jobs, then replace only
   Current API while retaining the recorded PostgreSQL and Redis containers.
9. Verify migrations, health, seeded state, immutable versions, original official
   PackageBlob retention, CAS, deployment, runtime reconciliation, and Integration
   Protocol migration coverage.
10. Restart Current API once, prove the data-service container IDs are unchanged,
   and verify the state again.

The job never runs `down -v` between Base and Current. Cleanup is scoped to the
Compose project label, its temporary worktree, and its temporary data directory.
It never runs Docker system, volume, or network prune commands.

## Disaster-recovery rehearsal gate

The `dr-rehearsal` job validates portable backup and recovery behavior in an
isolated A-to-B rehearsal. Recovery and migration runtime signals select this job
independently of `stateful-upgrade`; database, Updater, release-transition, and
deployment signals select Stateful Upgrade independently of DR. `force_full:
true` always selects both jobs.

## Base resolution

Base/Head resolution is shared by both gates:

- Pull request: `github.event.pull_request.base.sha` -> `github.sha`.
- Push: `github.event.before` -> `github.sha`.
- New-branch/all-zero push: `HEAD^` fallback.
- Direct Full CI dispatch: required `comparison_base_sha` -> required
  `candidate_sha`.
- Direct Release Gate dispatch: required `upgrade_base_sha` -> required
  `candidate_sha`.
- Pre-Merge dispatch: validated current `origin/main` -> validated exact PR head.
- `workflow_call`: the trusted Pre-Merge workflow supplies the validated base,
  candidate, and `force_full: true` inputs.
- Local invocation: explicit refs are preferred; otherwise `HEAD^`.

`HEAD^` is only a local convenience fallback. It is not proof of the currently
deployed production release. RC release validation must use the exact audited
release base required by the release workflow; Pre-Merge instead always uses the
live current `main` commit as its Base-to-Current comparison.

## Local commands

Run the static correctness and critical browser gates after installing the
project dependencies and building the frontend:

```bash
npm run lint
python -m ruff check --select E9,F63,F7,F82 backend bridges scripts
npm run build
npm run qa:critical
```

Resolve and audit the comparison refs:

```bash
python scripts/ci_refs.py --base <known-base-sha> --head HEAD
```

Run the artifact-only gate after building the official frontend artifact:

```bash
python scripts/pluginctl.py build watch-history-importer
python scripts/check_official_plugin_immutability.py --base <known-base-sha> --head HEAD --head-root .
```

Run the Docker upgrade gate when Bash and Docker Compose are available:

```bash
bash scripts/stateful-upgrade-gate.sh --base <known-base-sha> --head HEAD --current-root "$PWD"
```

The stateful script uses CI-only secrets and runner-temporary bind mounts. It is
not a production deployment script and must not be pointed at production data.
