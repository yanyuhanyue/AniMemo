# AniMemo v1.0 CI / Release Time-Efficiency Audit

Audit date: 2026-08-12. Scope: GitHub Actions evidence only; production and real
release operations were not run.

## Measured critical paths

| Evidence | Wall clock | Critical job | Critical execution |
| --- | ---: | --- | ---: |
| [PR Fast CI #31633960862](https://github.com/yanyuhanyue/AniMemo/actions/runs/31633960862) | 361 s | backend | 313 s |
| [PR Fast Release Gate #31633961002](https://github.com/yanyuhanyue/AniMemo/actions/runs/31633961002) | 127 s | stateful-upgrade | 116 s |
| [Pre-Merge Full #31634477142](https://github.com/yanyuhanyue/AniMemo/actions/runs/31634477142) | 356 s | full-regression / backend | 310 s |
| [main CI #31635026824](https://github.com/yanyuhanyue/AniMemo/actions/runs/31635026824) | 8 s | changed-files | 4 s |
| [main Release Gate #31635026828](https://github.com/yanyuhanyue/AniMemo/actions/runs/31635026828) | 27 s | post-merge-sanity | 8 s |
| [Performance Baseline #31610568595](https://github.com/yanyuhanyue/AniMemo/actions/runs/31610568595) | 1,612 s | isolated-resource-load | 1,590 s |

GitHub reported zero run-level queue delay for these samples. PR Fast job start
offsets were 25 s for classification, 43 s for the main fan-out, and 358 s for
the final aggregate; those offsets include dependency orchestration and runner
allocation and are not presented as pure queue time.

## Step evidence

On PR Fast CI #31633960862:

- backend dependency installation took 12 s across application and tooling
  requirements; the Django suite took 280 s of the 313 s job;
- frontend `npm ci` took 4 s, build 6 s, Chromium installation 24 s, and critical
  browser regression 22 s;
- PostgreSQL service initialization took 23 s, dependency installation 9 s,
  and concurrency tests 28 s;
- the two real AstrBot runtime jobs spent 48 s and 58 s installing runtime
  dependencies, but completed in parallel and were not the critical path.

On Release Gate #31633961002, Fresh Docker image build took 25 s and stack start
took 22 s; stateful Base-to-Current rehearsal took 107 s. On Pre-Merge Full
#31634477142, the backend suite again dominated at 278 s while Fresh Docker build
was 29 s and stateful rehearsal 93 s. No material artifact upload/download step
was on these gate critical paths.

## Decisions

| Area | Result | Decision |
| --- | --- | --- |
| Critical path audit | PASS | Optimize selection and authority first; full backend remains the measured full-gate constraint. |
| Cheap fail first | PASS | Changed-file classification and static fast-fail precede product fan-out. |
| Expensive test classification | PASS | LOW/STANDARD select affected jobs; HIGH adds Docker/stateful; CRITICAL adds Updater isolation. |
| Selection authority | PASS | Independent validators require selected success and unselected skip, fail-closed on schema/result drift. |
| Exact-SHA cross-run reuse | DEFERRED | Current result identity does not bind every workflow/config/lock invalidator. |
| Build-once cross-gate reuse | DEFERRED | It needs immutable digest/provenance transfer and would not shorten the measured backend critical path. |
| Test sharding | NOT BENEFICIAL | No stable low-risk shard plan was proven; splitting the 278-280 s Django suite now adds authority complexity. |
| Cache hit rate | INCONCLUSIVE | Setup actions use declared caches, but this sample does not expose a trustworthy aggregate hit/miss rate. |
| Sustained performance | PASS | Keep manual/RC/risk-triggered; its fixed 1,500 s load is intentionally not ordinary PR work. |

## Before / after accounting

The measured pre-hardening HIGH/CRITICAL machine path was approximately 717 s
when PR Fast CI (361 s), PR Fast Release Gate (127 s), and explicit Pre-Merge
Full (356 s) are counted as serial operator stages with parallel workflows
within each stage. The overlap-adjusted sum is an operator-path estimate, not a
single GitHub run duration.

The hardening removes false-green selection states and narrows HIGH Release Gate
work by reserving the Updater subset for CRITICAL changes. It does not claim a
stable wall-clock reduction before post-change GitHub evidence exists. Final
after-times and cache rate therefore remain **INCONCLUSIVE** rather than
invented. Future ordinary LOW/STANDARD patches target tens of minutes including
one authoritative Pre-Merge Full; full RC Readiness remains milestone-only.
