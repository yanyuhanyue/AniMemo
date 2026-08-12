# AniMemo v1.0 Performance Benchmark Contract

Date: 2026-08-12
Historical base: `179688c3478555f52ee66c78bfec103e1766047d`

This is the shared contract for the parallel Performance Baseline & Hardening workstreams. It defines measurement inputs and evidence shape; it does not claim benchmark results.

## Environment and data

- Authoritative API, database, resource and concurrency measurements run on Ubuntu with PostgreSQL and Redis in an isolated CI environment.
- Local Windows browser results are auxiliary. SQLite results cannot represent production database performance.
- Deterministic generated datasets are `SMALL` (50 journal entries), `MEDIUM` (1,000), and `LARGE` (10,000). Seed data includes bounded tags, years, statuses, metadata, watch history, supporting users and plugin records without committing a large JSON fixture.
- Dashboard scaling includes page 1, a middle page, and page 48.

## Repetition policy

- API/database probes: 2 warm-up runs followed by 10 measured runs.
- Production browser probes: 1 warm-up followed by 5 measured runs where practical.
- Report median and nearest-rank p95. Do not report p99 from these sample sizes.
- A before/after result is `INCONCLUSIVE` when the observed difference is within run-to-run noise.

## Load policy

- Lightweight concurrency levels: 1, 5, 10, and 20 users.
- Sustained scenario duration: 25 minutes.
- The workload is read-heavy/mixed normal usage. It must not use destructive production-like writes or unrealistic stress loads.

## Immediate hard failures

The first gate may fail on deterministic correctness/performance regressions: HTTP 5xx, exact duplicate critical initial requests without an allowlisted reason, N+1/query-count explosion, bundle explosion relative to the measured baseline, unbounded memory or Redis growth, and database connection exhaustion.

Latency is recorded as baseline evidence in the first version. No arbitrary percentage latency threshold is a hard gate until repeated runs establish stable variation.

## Finding schema

Each workstream records `ID`, `Proposed Severity`, `Area`, `Journey`, `Dataset`, `Evidence`, `Before`, `Root Cause`, `Suggested Fix`, `Contract Risk`, and `Owner`. Agents propose severity; the coordinator assigns final severity at the finding barrier.

Frozen contracts remain unchanged: API v1, Auth, Resource Identity, Plugin SDK v2, Integration Protocol v1, `DA-TD1-001`, and `DA-TD1-004`.
