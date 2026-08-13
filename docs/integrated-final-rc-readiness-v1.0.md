# AniMemo v1.0 Integrated Final RC Readiness

Final verdict: **NOT READY**

Evidence window: 2026-08-13 19:40 through 2026-08-14 01:30, Asia/Shanghai

Elapsed evidence run: approximately 5 hours 50 minutes
Production operations: **NOT RUN BY DESIGN**

## Authority and identity

| Item | Value |
| --- | --- |
| Repository | `yanyuhanyue/AniMemo` |
| Actual `origin/main` | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| Barrier A | `f49cbce6fee839609ca4ed04e091f82b37f3fc45` |
| Barrier B | `103c82055cffd3a5d21144a100b050b65fe44ac4` |
| Merge base with `origin/main` | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| Integrated candidate | `103c82055cffd3a5d21144a100b050b65fe44ac4` |
| Final application candidate | `306878de039cba6a706ce11c03ac9dab97448917` |
| Final readiness SHA | Set by the report-only commit containing this document |
| Draft PR | `#76`, `work/integrated-final-rc-readiness-20260813` to `main` |
| PR state | OPEN, DRAFT |

At final application-candidate freeze, local HEAD and the remote work branch both resolved to `306878de039cba6a706ce11c03ac9dab97448917`. Barrier B was zero commits behind `origin/main` and three commits ahead at the initial checkpoint; `origin/main` did not advance during the run.

The report-only readiness commit does not change the application candidate. Expensive code evidence remains bound to `306878d`; remote branch identity after the report commit is recorded separately as `FINAL_READINESS_SHA`.

## Barrier B evidence rebuild

Result: **PASS**. The exact Barrier B tree was rebuilt and exercised before candidate integration. Build, frontend tests, lint, plugin validation, Python compilation, Django checks, migration checks, provider/Bangumi configuration, origin/CORS/CSRF/host validation, namespace, browser migration, Bridge, Updater, Release, and CI-classifier coverage produced no RC0/RC1 mismatch.

No evidence mismatch was found between the previous Barrier B report and the rebuilt result. Two later candidate fixes addressed failures exposed only by exact remote RC environments:

1. The isolated performance seed now initializes its own `InstallationState`, allowing real installation-bound token issuance.
2. Official plugin sync migrates the known legacy ID `com.anime-journal.watch-history-importer` to `com.animemo.watch-history-importer` while preserving the project primary key, installations, data, and historical versions; unknown conflicts still fail closed.

## Exact-SHA remote evidence

| Workflow | Run | Head SHA | Result |
| --- | ---: | --- | --- |
| PR CI | `31722610999` | `306878d` | PASS |
| PR Release Gate | `31722611077` | `306878d` | PASS |
| Manual full CI | `31722761518` | `306878d` | PASS |
| Manual full Release Gate | `31722761578` | `306878d` | PASS |
| Performance | `31722761167` | `306878d` | PASS |
| Default-branch Pre-Merge Full Gate | `31722761423` | trusted workflow at `8727aa9` | **FAIL** |

The PR CI passed backend, frontend, PostgreSQL, plugins, bootstrap on Windows and Ubuntu, AstrBot Bridge, two AstrBot runtime versions, selection authority, and PR fast gate. The PR Release Gate passed updater-isolated, fresh Docker, stateful upgrade, and release authority at CRITICAL risk.

The Pre-Merge run correctly used the trusted workflow from `main`, but that workflow still injected legacy `ANIME_JOURNAL_*` values and omitted `ANIMEMO_PUBLIC_ORIGIN`. Candidate settings failed closed with:

```text
django.core.exceptions.ImproperlyConfigured:
ALLOWED_HOSTS 必须包含 ANIMEMO_PUBLIC_ORIGIN 的主机。
```

This is an authority contract mismatch, not a candidate pass. The PR status therefore contains a failing `pre-merge-authority` context and must remain Draft.

## Verification results

| Area | Result | Evidence |
| --- | --- | --- |
| Repository Evidence Rebuild | PASS | Exact refs, clean candidate, diff/compile checks |
| Barrier B Evidence | PASS | Rebuilt targeted and integration evidence |
| Remote exact-SHA CI | FAIL | Candidate CI/Release/Performance pass; required trusted Pre-Merge authority fails |
| Backend Full | PASS | 622 tests, 589 pass and 33 platform/database skips on GitHub; local run 586 pass and 36 skips; 0 failures |
| Frontend | PASS | Build, lint, 171/171 tests, critical browser regressions |
| Plugin SDK v2 | PASS | Manifest/package/runtime validation and official plugin tests |
| Integration Protocol v1 | PASS | Backend suite, PostgreSQL paths, stateful upgrade |
| Bridge | PASS | Bridge plus AstrBot `v4.27.2` and pinned runtime smoke |
| Updater | PASS | 142 isolated tests; Unix socket, fixed argv, recovery journal, compatibility, A-B-A behavior |
| Release | PASS | Release tests, classifier, manifest/checksum/provenance contracts, gate authority |
| Fresh Docker | PASS | Ephemeral build/boot/migration/bootstrap/health/first-run and D1A gate |
| Stateful Upgrade | PASS | Historical base to candidate; representative data and installation identity preserved after restart |
| Recovery | **FAIL** | Required execution NOT RUN: no complete isolated A backup to fresh B restore rehearsal |
| Failure Injection | **FAIL** | Updater/release injection is broad, but provider corrupt-ciphertext/wrong-key focused injection is missing; no single full required matrix report |
| First-run | PASS | State machine, TTL/attempt/race tests and real Docker setup lock |
| Provider Config | PASS | DB/env precedence, encrypted secret, masking, callback, and availability pass; the missing corrupt-ciphertext/wrong-key case is counted under Failure Injection |
| Namespace | PASS | Active owned legacy namespace 0; remaining matches classified as migration history, denylist, historical fact, or test fixture |
| DeepSec L1 | PASS | 60 raw findings manually triaged; 0 confirmed |
| DeepSec L2 | PASS | 7 raw findings; 6 FP/N/A, trusted-plugin network boundary deferred as non-blocking Medium |
| DeepSec L3 | FAIL | Final four changed files scanned, but full final-SHA repository L3 timed out |
| DeepSec Supply Chain | PASS | 0 findings |
| Spear | NOT RUN | Production/private targets forbidden by policy |
| Concurrency Probe | **FAIL** | Existing `1/5/10/20` plus sustained 5 passes; required long-task matrix absent |
| Structural Debt Review | PASS WITH ACCEPTED DEBT | TD0 0; two accepted TD1; new TD1 0 |
| Release Producer dry-run | **FAIL** | Required execution NOT RUN: workflow legally requires candidate to equal `origin/main` |
| RC to Stable artifact parity | **FAIL** | Required execution NOT RUN: depends on Release Producer dry-run outputs |

## Local and remote test detail

- Candidate local full backend: 622 tests, 586 PASS, 36 SKIP, 0 FAIL.
- Candidate GitHub backend: 622 tests, 589 PASS, 33 SKIP, 0 FAIL in 287.621 seconds.
- Frontend: 171 PASS, 0 FAIL, 0 SKIP; production build completed; critical auth/dashboard browser tests passed.
- Script suite: 186/186 PASS locally.
- Updater isolated: 142 tests PASS in 9.049 seconds.
- Local isolated SQLite performance command: 16 probes, all HTTP 200.
- Modified-file Ruff, `compileall`, and `git diff --check`: PASS.
- Whole-repository Ruff reported 472 pre-existing findings outside this RC fix scope; no result was hidden or relabeled as a candidate regression.

## Fresh install and upgrade

Fresh Docker used a disposable Compose project, PostgreSQL, Redis, and data root. It built the candidate images, applied migrations, ran bootstrap, reached health, completed the real one-time setup API, verified `/setup` lock, and exercised the fresh external-collection routes without production credentials.

Stateful upgrade preserved or migrated users, journal data, watch history, provider/external-account metadata, external media identity, SiteConfig/metadata source, Plugin project/version/deployment/CAS/data/installation, Integration state, and initialized InstallationState. Verification passed both before and after a scoped current-API restart.

This is not a disaster-recovery restore. The current backup producer creates and verifies a PostgreSQL `pg_dump` gzip, while the complete recovery set for media, plugin CAS/runtime state, updater state, authentication restoration, and fresh-instance restore verification is not implemented or rehearsed. Recovery is therefore fail-closed **NOT RUN/FAIL**.

## Failure injection assessment

Release and Updater tests cover invalid or mismatched manifest/provenance/digest data, fixed-source fetch/pull errors, backup failure, migration/bootstrap/health/switch failures, stable-window 5xx/critical logs, interrupted operations, concurrent update locks, corrupted durable state, CURRENT/PREVIOUS slot behavior, unsafe downgrade, incompatible previous applications, credential redaction, and crash recovery without replaying migration.

However, the required provider-secret failure injection is incomplete. The effective provider path catches credential cipher/decryption errors and makes OAuth unavailable, but no focused test injects corrupt database ciphertext and a wrong encryption key through the complete staff/effective-configuration surface. Because the requested matrix names that case explicitly, Failure Injection cannot be marked PASS.

## DeepSec result

DeepSec `0.2.0` scanned a 721-file tracked-only archive. Raw artifacts remain outside the repository. Summary:

- L1: 60 raw, all false positive/not applicable.
- L2: 7 raw, 6 false positive/not applicable and 1 deferred Medium.
- Focused final-delta L3: 10 raw, all false positive/not applicable.
- Supply chain: 0.
- Confirmed Critical: 0.
- Confirmed High: 0.
- Total false positive/not applicable: 76.
- Deferred: 1 Medium, Plugin Host arbitrary outbound URL under the trusted in-process v1 model.

See `docs/security/deepsec-final-rc-audit-v1.0.md` for the sanitized triage.

## Concurrency result

Performance run `31722761167` passed the current regression contract. The 25-minute concurrency-5 workload completed 19,845 requests with zero errors, 29.059 ms p50 and 57.723 ms p95. Burst concurrency 20 completed 240 requests with zero errors, 196.676 ms p50 and 543.808 ms p95. PostgreSQL peaked at 14 of 100 connections.

The mandatory `20/40/60 x 0/2/4/8 long operations` matrix did not run. P99, 429, long-operation latency, and worker saturation for that matrix are absent. Comfortable capacity is proven only for the existing normal workload; degraded point is observed at burst 20; saturation is not established; Job Queue is **INCONCLUSIVE**.

See `docs/concurrency-long-task-isolation-v1.0.md`.

## Structural debt

TD0: **0**.

TD1: **2 accepted exceptions**, no new/unresolved TD1:

- Plugin SDK/runtime lacks a serializable worker/RPC boundary.
- Integration receipts may remain permanently PENDING after crash.

TD2: **15 tracked concerns**, including the newly explicit complete DR backup/restore-set gap.

TD3: **5**.

See `docs/v1.0-structural-debt-last-chance.md`.

## Release decision and blockers

RC0 count: **1 readiness blocker**: the mandatory recovery contract/rehearsal is absent. Confirmed code/security RC0 defects: **0**.

RC1 count: **3 unresolved readiness blockers**: Pre-Merge authority contract mismatch, incomplete failure injection, and incomplete long-task isolation matrix.

RC2 count: **1 deferred security boundary** plus documented TD2/TD3 backlog.

Release Producer dry-run and RC-to-Stable parity are two additional mandatory evidence gates marked FAIL because execution was NOT RUN; they are not assigned a defect severity until the candidate is legally on `main` and the producer can execute.

The four stop conditions before a real Final RC are:

1. Implement and rehearse the complete isolated disaster-recovery restore contract, including post-restore setup lock and authentication handling.
2. Add provider corrupt-ciphertext/wrong-key injection evidence and close the required failure-injection matrix.
3. Run the minimum long-task isolation matrix and make an evidence-based Job Queue decision.
4. Align the trusted default-branch Pre-Merge workflow with the candidate namespace/public-origin contract, then obtain a passing exact-candidate authority run.

After those are closed and the candidate is legally on `main`, run the Release Producer with `dry_run=true`, `push=false` behavior, and prove RC-to-Stable commit/API/Web digest parity without rebuilding.

## Operations explicitly not run

| Operation | Result |
| --- | --- |
| SSH to VPS | NOT RUN |
| Production mutation or deployment | NOT RUN |
| Cloudflare / DNS / R2 mutation | NOT RUN |
| Real Bangumi application mutation | NOT RUN |
| Production Postgres / Redis access | NOT RUN |
| Production Acceptance | NOT RUN |
| Real Final RC | NOT RUN |
| Tag creation | NOT RUN |
| GitHub Release | NOT RUN |
| GHCR publish | NOT RUN |
| Stable promotion | NOT RUN |
| Merge to `main` | NOT RUN |

## Final verdict

**NOT READY**.

The frozen candidate has strong non-production evidence and no confirmed Critical or High security finding. It does not satisfy the declared READY criteria because Recovery and Failure Injection are incomplete, the required long-task isolation matrix is absent, trusted Pre-Merge authority fails, and Release Producer / RC-to-Stable parity is not legally runnable before the candidate reaches `main`.
