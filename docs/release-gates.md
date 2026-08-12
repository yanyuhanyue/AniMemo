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
   subsystem gates, and immediate full fan-out for CI/deployment/high-risk
   changes.
3. **Pre-Merge Full Gate:** one full regression plus Release Gate for the exact
   final PR head against current `main`.
4. **main:** lightweight post-merge verification only.
5. **RC:** the full RC Release Gate, build-once artifacts, and immutable
   promotion described by the release contract.

## PR Fast Gate

Ordinary PR updates use `scripts/ci_classify.py` to select the affected gates.
Docs-only, frontend-only, and backend-only changes do not automatically pay for
the complete matrix. CI workflows, deployment files, Dockerfiles, dependency
definitions, release scripts, classifier changes, shared contracts, and other
high-risk combinations force the full CI and Release Gate immediately.

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

Content identity is not archive identity. For example, the same payload may have
content digest `CCC` while a deflated archive has SHA `AAA` and a stored archive
has SHA `BBB`. The immutable version remains unchanged because its payload is the
same. CAS still addresses the actual archive bytes, and an already-published
official version retains the original `AAA` blob instead of being rewritten to
`BBB`.

## Fresh Docker release gate

The `docker` job validates `EMPTY DATABASE -> CURRENT RELEASE` with the base
production Compose plus `deploy/docker-compose.build.yml`. It builds API/Web,
starts PostgreSQL and Redis, runs the explicit target `migration` and
`bootstrap` jobs, then starts API/Web and checks health, frontend and fresh
contract state. API startup does not own migration orchestration.

## Stateful upgrade gate

The `stateful-upgrade` job validates `BASE RELEASE -> CURRENT RELEASE` without
recreating persistent state:

1. Create an isolated Compose project and runner-temporary data root.
2. Check out Base in a detached Git worktree.
3. Build/start Base PostgreSQL, Redis, and API using the Base release's audited
   historical behavior, then record PostgreSQL and Redis container IDs.
4. Seed a user, journal entry, user plugin installation, `watch_history`
   `PluginData`, official project/version/blob/deployment, CAS, and runtime.
5. Build Current with the current build override.
6. Run Current's explicit `migration` and `bootstrap` jobs, then replace only
   Current API while retaining the recorded PostgreSQL and Redis containers.
7. Verify migrations, health, seeded state, immutable versions, original official
   PackageBlob retention, CAS, deployment, runtime reconciliation, and Integration
   Protocol migration coverage.
8. Restart Current API once, prove the data-service container IDs are unchanged,
   and verify the state again.

The job never runs `down -v` between Base and Current. Cleanup is scoped to the
Compose project label, its temporary worktree, and its temporary data directory.
It never runs Docker system, volume, or network prune commands.

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
