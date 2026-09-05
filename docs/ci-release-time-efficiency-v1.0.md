# AniMemo v1.0 CI / Release Time-Efficiency Audit

Audit date: **2026-08-13 (Asia/Shanghai)**. Scope: read-only GitHub Actions
evidence and local classifier/authority validation. No production access,
deployment, image publication, workflow dispatch, commit, push, or PR action was
performed by this audit.

Audited main identity: `8727aa97dc092d12e4a4abb15b85ce1f46d1020d`.
The required merge contexts remain `pr-fast-gate` and `pre-merge-authority`.
`.github/workflows/**` is outside this change set and was not edited.

## Run evidence

Times below are GitHub run wall-clock durations. The run timestamps were checked
against Shanghai time; the dated PR samples below completed on August 12 or
August 13, 2026 Shanghai time.

| Sample | Run | Head / event | Wall | Critical path evidence |
| --- | --- | --- | ---: | --- |
| Before CI | [31633960862](https://github.com/yanyuhanyue/AniMemo/actions/runs/31633960862) | `e2316876…` / pull request | 361 s | `backend` 313 s; Django suite 280 s |
| Before Release Gate | [31633961002](https://github.com/yanyuhanyue/AniMemo/actions/runs/31633961002) | `e2316876…` / pull request | 127 s | `stateful-upgrade` 116 s |
| Before Pre-Merge | [31634477142](https://github.com/yanyuhanyue/AniMemo/actions/runs/31634477142) | pre-merge candidate | 356 s | full-regression `backend` 310 s; Django 278 s |
| Before Release Producer | [31588385877](https://github.com/yanyuhanyue/AniMemo/actions/runs/31588385877) | release candidate | 386 s | full-CI `backend` 296 s; read-only dry-run 53 s |
| Performance auxiliary | [31610568595](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610568595) | performance candidate | 1,612 s | isolated load 1,590 s; not ordinary merge path |
| After CI | [31663224867](https://github.com/yanyuhanyue/AniMemo/actions/runs/31663224867) | `76eb70b…` / pull request | 367 s | `backend` 324 s; Django suite 291 s |
| After Release Gate | [31663224855](https://github.com/yanyuhanyue/AniMemo/actions/runs/31663224855) | `76eb70b…` / pull request | 111 s | `stateful-upgrade` 93 s |
| After Pre-Merge | [31663655749](https://github.com/yanyuhanyue/AniMemo/actions/runs/31663655749) | pre-merge candidate | 376 s | full-regression `backend` 322 s; Django 289 s |
| Exact-main CI | [31664002599](https://github.com/yanyuhanyue/AniMemo/actions/runs/31664002599) | `8727aa97…` / push | 18 s | changed-files and selection authority only |
| Exact-main Release Gate | [31664002608](https://github.com/yanyuhanyue/AniMemo/actions/runs/31664002608) | `8727aa97…` / push | 28 s | lightweight classification/sanity; product release jobs skipped |

The exact-main runs are post-merge lightweight verification. They are not a
stable before/after peer for the PR Fast + Release Gate + Pre-Merge Full path.
There is no after-date Release Producer dry-run equivalent in the queryable
sample.

## Critical path and before/after accounting

The pre-hardening operator-path estimate is:

```text
max(PR Fast CI 361 s, PR Fast Release Gate 127 s) + Pre-Merge Full 356 s
= 717 s
```

The observed hardening sample is:

```text
max(PR Fast CI 367 s, PR Fast Release Gate 111 s) + Pre-Merge Full 376 s
= 743 s
```

The observed difference is **+26 s (+3.6%)**, but the candidates, changed-file
sets, test quantities, and execution conditions are not controlled enough for a
causal comparison. Therefore **before/after wall clock: INCONCLUSIVE**. The
backend/Django suite remains the measured Full Gate critical path; the shorter
after Release Gate stateful job does not establish an end-to-end speedup.

Release Producer before/after is also **INCONCLUSIVE**: run `31588385877`
contains a 53-second read-only release dry-run, but no comparable after run was
available. The exact-main 18-second CI and 28-second Release Gate runs only prove
that lightweight post-merge selection is working.

## Cheap fail first

| Layer | Evidence | Result |
| --- | --- | --- |
| Changed-file classification | `changed-files` precedes product fan-out; 6–8 s in sampled runs | PASS locally |
| `ci-fast-fail` | Classifier validation and `git diff --check` run before product jobs on PR samples | PASS locally |
| Product static checks | Ruff/lint/migration/OpenAPI checks remain inside product jobs | **FAIL for end-to-end cheap-fail-first** |
| Release topology | Updater/Docker/stateful jobs can start while CI static/product jobs are still running | **FAIL for end-to-end cheap-fail-first** |

Overall **Cheap fail first: FAIL**. The current workflow topology does not prove
that all cheap failures are resolved before expensive fan-out. Closing this item
requires the shared workflow change owned by the coordinator; it is not silently
marked PASS by this non-workflow patch.

## Duplicate builds and reuse

Qualitative duplicate build work remains confirmed in both samples:

- the frontend job performs its own frontend build;
- Fresh Docker validation builds API/Web images in its release-gate trust
  boundary;
- Stateful Upgrade builds Base and Current independently to preserve the
  Base-to-Current stateful boundary;
- cross-gate immutable digest/provenance transfer is not established.

The numeric **duplicate-build before/after delta is INCONCLUSIVE** because the
queryable runs do not expose a stable, equivalent build-count denominator. The
qualitative result is **duplicate builds remain; no reduction is evidenced**.
Stable promotion does not rebuild the already-produced image, and the Release
Producer path keeps build → rehearse → push on the same release image.

Build-once cross-gate reuse remains **DEFERRED**. Exact-SHA cross-run reuse also
remains **DEFERRED**; this patch introduces no unsafe reuse and does not weaken
`pre-merge-authority`.

## Cache evidence

The sampled frontend jobs reported primary-key npm cache hits, and sampled
Python jobs reported primary-key pip cache hits. This establishes configured
cache activity for those jobs, not an aggregate hit-rate measurement.

The evidence does not provide a trustworthy request/miss denominator for all
jobs. Playwright browser installation, AstrBot runtime dependency setup, some
plugin/bootstrap/bridge installs, and Docker BuildKit cache reuse are not shown
as one complete comparable cache policy. Therefore:

- Cache configuration audit: **PARTIAL**.
- Cache hit rate before/after: **INCONCLUSIVE**.
- Claimed cache time saving: **not established**.

## Decision matrix

| Area | Result | Evidence-based decision |
| --- | --- | --- |
| Exact main identity | PASS | All local and queried main identity checks resolve to `8727aa97…`. |
| Required authority | PASS | `pr-fast-gate` and `pre-merge-authority` remain the required merge contexts. |
| Critical path audit | PASS | Backend/Django remains the dominant Full Gate path. |
| Before/after wall clock | **INCONCLUSIVE** | 717 s versus 743 s is not controlled-comparable. |
| Release dry-run before/after | **INCONCLUSIVE** | Only the before Release Producer dry-run is available. |
| Cheap fail first | **FAIL** | Current workflow topology does not front-load all cheap static failures. |
| Expensive-test allocation | PASS | Risk selection and authority tests enforce selected success/unselected skip. |
| Duplicate builds | CONFIRMED / no reduction evidenced | Cross-boundary rebuilds remain; numeric delta is INCONCLUSIVE. |
| Build-once cross-gate reuse | DEFERRED | Requires immutable digest and provenance binding. |
| Exact-SHA cross-run reuse | DEFERRED | HEAD-only reuse remains forbidden. |
| Cache configuration | PARTIAL | npm/pip hits observed; coverage is incomplete. |
| Cache hit rate | **INCONCLUSIVE** | No reliable global denominator. |
| Sustained performance | AUXILIARY | 1,590-second isolated load is not ordinary PR work. |

## RC disposition for this working tree

- **RC1 classifier semantic coverage: CLOSED locally.** Tests now require
  first-run public entrypoints/setup routes to be CRITICAL, release-image
  rehearsal to be CRITICAL, backend route modules to be at least HIGH, and plugin
  schema paths to be HIGH. Same-family glob and negative-boundary cases are
  covered, with release authority integration tests.
- **RC1 evidence/report closure: CLOSED locally.** Current queryable runs,
  critical-path observations, duplicate-build findings, cache limitations, and
  all non-comparable before/after fields are now recorded explicitly. An
  `INCONCLUSIVE` measurement is an evidence limitation, not an invented PASS.
- **RC1 cheap-fail-first topology: OPEN.** It requires a workflow change and is
  outside this write set.
- **RC0: 0 open.** Required authority remains fail-closed and no unsafe reuse was
  introduced.

This document records the local closure and remaining coordinator-owned item;
it is not a release authorization.
