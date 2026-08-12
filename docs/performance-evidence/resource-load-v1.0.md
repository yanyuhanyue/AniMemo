# AniMemo v1.0 Resource / Load Evidence — Wave 1

Date: 2026-08-12

Workstream: Resource / Load / Sustained Runtime

Base contract commit: `0f865f4c5b43b36de1fde847ee8ac0d6f99ce6ab`

## Scope and authority

This workstream adds measurement harness and evidence only. It does not change frontend/backend product behavior, migrations, CI authority, release/updater behavior, or frozen v1 contracts.

Authoritative results require an isolated Ubuntu runner with PostgreSQL and Redis. This Windows machine has no Docker runtime, so no PostgreSQL/Redis/resource/load result was generated locally. Unit tests below validate the harness and safety boundaries only.

| Evidence | Status | Authority |
| --- | --- | --- |
| Harness unit tests | PASS (14/14) | Tool correctness only |
| 1-user isolated load | NOT RUN | Await Ubuntu Actions isolated stack |
| 5-user isolated load | NOT RUN | Await Ubuntu Actions isolated stack |
| 10-user isolated load | NOT RUN | Await Ubuntu Actions isolated stack |
| 20-user isolated load | NOT RUN | Await Ubuntu Actions isolated stack |
| 25-minute sustained load | NOT RUN | Await Ubuntu Actions isolated stack |
| API/Web/PostgreSQL/Redis resources | NOT RUN | Await Ubuntu Actions isolated stack |

No production host, production credential, SSH session, production database, R2 write, deploy, or update was used.

## Implemented harness

`scripts/perf/isolated_run.py` is the preferred single entry point. It:

- requires explicit `--confirm-isolated`, base URL, generated entry ID, Compose project, PostgreSQL database/user, and test credential environment-variable names;
- rejects `re-anime.cc`, all its subdomains, and `45.207.221.83` before network traffic;
- performs one explicit user login and optional Staff login before measured traffic, then reuses the issued test token so authentication throttling is not mistaken for read-load behavior;
- refreshes the shared isolated session under a lock when the 10-minute access token expires, preventing a concurrent re-login stampede during the 25-minute run;
- loops read-only Dashboard, filter/search, entry detail, watch history, enabled plugins, and optional Staff health journeys;
- runs concurrency levels 1, 5, 10, and 20, then a 25-minute sustained scenario;
- records request count, error rate, HTTP 5xx, transport errors, p50, p95, throughput, response bytes, and per-journey summaries;
- samples API/Web/PostgreSQL/Redis container CPU and memory through `docker stats`;
- samples PostgreSQL active/max connections with read-only `pg_stat_activity`/`current_setting` SQL;
- samples Redis memory and key count with read-only `INFO memory` and `DBSIZE`;
- rejects the fixed production Compose project/container names in addition to rejecting the production HTTP hosts;
- hard-fails only deterministic conditions: HTTP 5xx, unexpected statuses, recognized connection exhaustion, database max-connection exhaustion, sustained monotonic memory/Redis/key growth beyond explicit absolute limits, or incomplete resource sampling;
- records latency without a percentage latency hard gate.

The scenario performs only `GET` product requests after the normal isolated-test login. Passwords, OTP values, and optional Turnstile test tokens are read from environment variables and are not accepted as CLI values or written to reports.

## Authoritative isolated command

The coordinator-owned Ubuntu Actions workflow can run the existing isolated Compose stack and deterministic seed, export generated test credentials, then invoke:

```bash
python -m scripts.perf.isolated_run \
  --confirm-isolated \
  --base-url http://127.0.0.1:8088 \
  --username animemo-perf-user \
  --password-env ANIMEMO_PERF_PASSWORD \
  --staff-username animemo-perf-staff \
  --staff-password-env ANIMEMO_PERF_STAFF_PASSWORD \
  --staff-otp-env ANIMEMO_PERF_STAFF_OTP \
  --entry-id "$ANIMEMO_PERF_ENTRY_ID" \
  --search-term anime \
  --compose-project "$COMPOSE_PROJECT_NAME" \
  --postgres-user "$POSTGRES_USER" \
  --postgres-database "$POSTGRES_DB" \
  --duration-seconds 1500 \
  --resource-interval-seconds 15 \
  --output artifacts/performance/resource-load.json
```

If the isolated Compose file does not use `${COMPOSE_PROJECT_NAME}-{api,web,postgres,redis}`, pass the four explicit `--*-container` options. Do not point this command at a shared host or production.

The 512 MiB API-memory, 256 MiB Redis-memory, and 50,000-key growth defaults are first-pass absolute runaway sentinels, not latency budgets. The authoritative baseline should retain the raw samples and may tighten these only after repeated isolated runs establish normal variance.

## Local verification

```text
python -m unittest scripts.tests.test_perf_load_harness scripts.tests.test_perf_load_resources -v
PASS — 14 tests

python -m py_compile scripts/perf/load_harness.py scripts/perf/resource_sampler.py scripts/perf/isolated_run.py
PASS

python scripts/perf/load_harness.py --help
PASS

python scripts/perf/resource_sampler.py --help
PASS

python scripts/perf/isolated_run.py --help
PASS
```

## Findings

No runtime performance finding is asserted because the authoritative PostgreSQL/Redis run is **NOT RUN**. When the isolated run produces evidence, any finding must use the shared schema below and keep severity explicitly proposed until the coordinator's finding barrier:

```text
ID: PERF-RES-xxx
Proposed Severity: PERF0 / PERF1 / PERF2 / PERF3
Area:
Journey:
Dataset:
Evidence:
Before:
Root Cause:
Suggested Fix:
Contract Risk:
Owner:
```

Current measured findings: **NONE — AUTHORITATIVE RUN NOT RUN**.

## Limitations

- The harness measures small-scale stability, not capacity. It intentionally stops at 20 concurrent virtual users.
- A shared access token models concurrent reads for one seeded account; it avoids login-throttle noise but does not model many independently authenticated accounts.
- Staff health can include its existing external dependency checks; that latency is recorded as journey evidence but has no first-version latency gate.
- Container metrics are point samples, not kernel-level profiling.
- Resource growth requires at least four monotonically non-decreasing samples plus an absolute growth limit before being called runaway; raw samples remain available for coordinator review.

## Production boundary

```text
PRODUCTION DEPLOY: NOT RUN
PRODUCTION UPDATE: NOT RUN
PRODUCTION PERFORMANCE TEST: NOT RUN
PRODUCTION DATABASE: NOT RUN
R2 PRODUCTION WRITE: NOT RUN
SSH: NOT RUN
```
