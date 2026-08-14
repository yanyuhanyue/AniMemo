# AniMemo v1.0 Integrated Final RC Readiness

Final verdict: **NOT READY**

Evidence window: 2026-08-13 19:40 through 2026-08-14 06:44, Asia/Shanghai (`2026-08-13T11:40Z` through `2026-08-13T22:44Z`).

Elapsed evidence run: approximately 11 hours 4 minutes. This exceeded the requested 8-hour absolute stop. No additional scan, fix, or third authority attempt was started after the two-attempt limit was exhausted.

Production operations: **NOT RUN BY DESIGN**.

## Authority and identity

| Item | Value |
| --- | --- |
| Repository | `yanyuhanyue/AniMemo` |
| Actual `origin/main` | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| Barrier A | `f49cbce6fee839609ca4ed04e091f82b37f3fc45` |
| Barrier B | `103c82055cffd3a5d21144a100b050b65fe44ac4` |
| Merge base with `origin/main` | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| Initial integrated baseline | `103c82055cffd3a5d21144a100b050b65fe44ac4` |
| Final application candidate | `085657249c5d174e17dac7ff6dc0797f3179165a` |
| Final readiness SHA | Report-only commit following `0856572`; record from the branch/PR head after this report is committed |
| Draft PR | [#76](https://github.com/yanyuhanyue/AniMemo/pull/76), `work/integrated-final-rc-readiness-20260813` to `main` |
| PR state at checkpoint | OPEN, DRAFT, BLOCKED |

Before the report-only commit, local HEAD, the remote work branch, and PR #76 HEAD all resolved to exact candidate `085657249c5d174e17dac7ff6dc0797f3179165a`. `origin/main` remained `8727aa9`; the branch was 0 behind and 11 commits ahead. The report-only commit does not change the application candidate or make old code evidence apply to a new code tree.

## Barrier B evidence rebuild

Result: **PASS**.

The exact Barrier B tree was rebuilt before integration. Build, frontend tests, lint, plugin validation, Python compilation, Django checks, migration checks, provider/Bangumi configuration, origin/CORS/CSRF/host validation, namespace, browser migration, Bridge, Updater, Release, and CI-classifier coverage produced no RC0/RC1 evidence mismatch.

Later candidate commits closed exact-environment gaps in DR authentication evidence, path safety coverage, CI authority selection, plugin identity binding, performance capacity evidence, and workflow contracts. They do not change the Barrier B result; they define the final candidate that required a new authority pass.

## Exact-SHA remote evidence

| Workflow | Run | Authority / head | Result |
| --- | ---: | --- | --- |
| PR CI | [31745442987](https://github.com/yanyuhanyue/AniMemo/actions/runs/31745442987) | candidate `0856572` | PASS |
| PR Release Gate | [31745443020](https://github.com/yanyuhanyue/AniMemo/actions/runs/31745443020) | candidate `0856572` | PASS |
| Performance Baseline | [31745479369](https://github.com/yanyuhanyue/AniMemo/actions/runs/31745479369) | candidate `0856572` | PASS |
| Trusted Pre-Merge Full Gate | [31745482956](https://github.com/yanyuhanyue/AniMemo/actions/runs/31745482956) | trusted workflow at main `8727aa9`, candidate snapshot `0856572` | **FAIL** |
| Release Producer dry-run track | [31745485923](https://github.com/yanyuhanyue/AniMemo/actions/runs/31745485923) | candidate `0856572` | **FAIL / CANCELLED** |

The candidate-controlled PR CI passed backend, frontend, PostgreSQL, plugins, bootstrap on Windows and Ubuntu, AstrBot Bridge, two AstrBot runtime versions, CI selection authority, and PR fast gate. The PR Release Gate passed updater isolation, fresh Docker, main-to-candidate stateful upgrade, full A-to-B DR rehearsal, and release-gate selection authority. Performance and its aggregate regression gate passed.

The trusted Pre-Merge run failed closed. The reusable Release Gate loaded from protected `main` does not contain the candidate's required `dr-rehearsal` job. The candidate compatibility logic observed the outer event as `workflow_dispatch`, not `workflow_call`, so it did not apply the intended narrow compatibility path. Authority therefore reported the required `dr-rehearsal` job missing. This is not a candidate PASS and leaves the trusted merge authority red.

The Release Producer dry-run did not reach artifact construction or parity verification. Its historical `6452b3d...` to candidate stateful-upgrade path twice stalled after the historical base API started and reached the serious-attempt time limits (30 minutes, then 40 minutes). The final exact-candidate run was cancelled with `stateful-upgrade` incomplete; `read-only-release-dry-run` and all publish jobs remained skipped. The two-attempt rule is exhausted.

## Final pass matrix

| Area | Result | Evidence |
| --- | --- | --- |
| Repository Evidence Rebuild | PASS | Exact refs, merge base, branch identity, and candidate freeze verified |
| Barrier B Evidence | PASS | Rebuilt exact Barrier B targeted and integration evidence |
| Remote exact-SHA CI | **FAIL** | Candidate CI/Release/Performance pass; mandatory trusted Pre-Merge authority fails |
| Backend Full | PASS | Exact candidate PR CI backend and PostgreSQL jobs pass |
| Frontend | PASS | Build, lint, tests, production probe, and bootstrap paths pass |
| Plugin SDK v2 | PASS | Manifest/package/runtime validation and official plugin tests pass |
| Integration Protocol v1 | PASS | Backend, Bridge, state persistence, and upgrade evidence pass |
| Bridge | PASS | Bridge plus AstrBot `v4.27.2` and pinned runtime smoke pass |
| Updater | PASS | Isolated updater, path/argv/state/compatibility and failure boundaries pass |
| Release | **FAIL** | Candidate release contract passes, but trusted authority and producer authority fail |
| Fresh Docker | PASS | Disposable build, boot, migration, setup, health, and persistence pass |
| Stateful Upgrade | **FAIL** | Main-to-candidate path passes; mandatory historical producer path times out |
| Recovery | PASS | Full isolated A backup, destroy-scoped A, fresh B restore, data/media/state verification, `/setup` locked |
| Failure Injection | PASS | Updater/release/DR/provider failure paths fail closed, including corrupt credential handling |
| First-run | PASS | Setup-code state machine, limits, races, plaintext removal, and post-restore lock pass |
| Provider Config | PASS | Encrypted secret, masking, DB-over-env, fallback, callback, availability, and failure behavior pass |
| Namespace | PASS | Active owned legacy namespace 0; remaining matches classified |
| DeepSec L1 | PASS | Broad tracked-source analysis executed; confirmed Critical 0 |
| DeepSec L2 | **FAIL** | Broad analysis identified a confirmed grouped release-authority trust-boundary High |
| DeepSec L3 | **FAIL** | Exact delta analysis reproduced the candidate self-certification High group |
| DeepSec Supply Chain | NOT RUN | Not rerun in the final pass; mutable action tags remain deferred debt |
| Spear | NOT RUN | Production/private targets forbidden by policy |
| Concurrency Probe | PASS | Required `20/40/60 x 0/2/4/8` matrix completed with active resource sampling |
| Structural Debt Review | **FAIL / BLOCKED** | No TD0, but trusted release authority is a new unresolved TD1/RC1 |
| Release Producer dry-run | **FAIL** | Historical stateful upgrade timed out; dry-run artifact job did not execute |
| RC to Stable artifact parity | **FAIL / NOT EVIDENCED** | Producer never reached artifact/parity jobs |

## Recovery and failure injection

The final PR Release Gate executed the complete isolated DR contract. Instance A contained representative users, staff state, SiteConfig, journal and watch history, provider/external-account metadata, external media identity, plugin state, Integration state, installation identity, and media references/files. The backup set was verified, only the disposable A project/root was destroyed, a fresh B instance restored the set, `/setup` remained locked, restored authentication identity was rotated, and representative state was checked after restore.

The DR workflow also includes direct backup and recovery-path tests. The final candidate checks that the pre-restore access token remains unexpired before proving authentication-epoch rejection, and the authoritative Release Gate includes the same recovery-path safety module as the standalone DR workflow.

Failure-injection coverage is accepted as PASS for the requested non-production scope: invalid or inconsistent release identity, pull/backup/migration/bootstrap/health/switch/stable-window failures, interruption and durable-state corruption, CURRENT/PREVIOUS mismatch, unsafe downgrade, incompatible previous, credential configuration failure, and provider secret decryption failure remain fail closed.

## DeepSec result

DeepSec `2.3.5` produced two tracked-source runs against the final evidence set:

- Broad run `20260813195454-71f63d8979a92b46`: 124 analyses and 114 report findings; report severity counters were Critical 0, High 2, Medium 65, High Bug 1, Bug 46.
- Exact delta run `20260813222055-e85e738b3a20c3c5` for `756bacd..0856572`: 6 changed files and 17 new analysis findings; report/export counters were Critical 0, High 3, Medium 18, High Bug 1, Bug 4.

The raw High records overlap. They establish one grouped concern: release classification, authority, and official plugin identity checks execute candidate-controlled logic while certifying that candidate. The intended trusted-main compensation failed in this exact authority pass, so the group is not dismissed as a false positive. It is recorded as one confirmed release-blocking High / RC1.

Confirmed Critical: **0**. Confirmed High: **1 grouped trust-boundary issue**. The artifact-permission High Bug is a false positive because upload/download artifact jobs succeeded. Hardcoded credential findings are synthetic isolated CI fixtures, not production secrets. Mutable GitHub Action tags and backup retention are real deferred hardening/operational debt. Supply-chain scanning was **NOT RUN** in the final pass, and Spear was **NOT RUN**.

## Concurrency result

Performance run `31745479369` completed the mandatory `20/40/60 normal users x 0/2/4/8 synchronous long operations` matrix against a fake 1.2-second provider with network disabled. All 7,722 requests returned HTTP 200: 0 timeouts, 0 HTTP 5xx, 0 HTTP 429, and 0 transport errors. Every matrix cell had active Docker/PostgreSQL/Redis resource sampling.

Four long operations did not cause severe starvation or errors. Eight long operations occupied the full configured `2 workers x 4 threads` synchronous capacity and produced clear p95/p99 degradation: at 20 users p95 increased from 325.316 ms to 1173.350 ms and p99 from 415.175 ms to 1375.156 ms; at 40 users p95 increased from 609.984 ms to 1197.285 ms and p99 from 766.456 ms to 1469.530 ms; at 60 users p95 increased from 848.891 ms to 1549.515 ms and p99 from 967.935 ms to 1811.623 ms.

Comfortable capacity: **up to four concurrent synchronous long operations in the tested envelope, with degradation at the 60-user edge but no hard failure**.

Degraded but usable: **eight long operations**.

Saturation boundary: **eight synchronous long operations, equal to configured worker-thread capacity**.

Job Queue decision: **DEFER TO v1.1**. Do not add Celery, RabbitMQ, Kafka, or a new worker architecture during v1.0 closure; retain the measured eight-operation degraded boundary and revisit with the Background Job contract.

## Structural debt and severity accounting

RC0 count: **0**.

RC1 count: **3 grouped release blockers**:

1. Trusted Pre-Merge authority fails because trusted `main` and candidate reusable Release Gate contracts disagree about `dr-rehearsal`.
2. Historical stateful upgrade stalls in Release Producer, so dry-run artifacts and RC-to-Stable parity are unavailable.
3. DeepSec release/plugin self-certification findings form one confirmed candidate-controlled authority trust-boundary High; the intended trusted-main compensation did not pass.

RC2 count: **3 grouped deferred categories**: mutable action tags/supply-chain pinning, backup retention policy, and non-production workflow/script hardening findings that do not create a current production credential or data-loss path.

TD0: **0**. Accepted v1.0 TD1 exceptions remain the serializable Plugin SDK/Runtime v3 boundary and PENDING Integration receipt liveness. One new unresolved TD1 is the trusted release-authority boundary. Background Job remains deferred to v1.1 based on the completed capacity matrix.

## Stop conditions before a real Final RC

1. Close the trusted-main reusable Release Gate compatibility gap and obtain a passing Trusted Pre-Merge authority run for the exact code candidate.
2. Diagnose and fix the historical `6452b3d...` stateful-upgrade hang, then rerun Release Producer dry-run within the two-attempt policy of a new candidate cycle.
3. Produce the dry-run manifest/checksums/provenance and prove RC commit/API/Web digests are identical to Stable without rebuilding.
4. Bind release/plugin classification and integrity authority to trusted code, or provide an equivalent protected authority that independently recomputes candidate claims.

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

Recovery, failure injection, and the required capacity matrix now pass, and confirmed Critical remains zero. AniMemo v1.0 still does not satisfy the declared READY criteria because Trusted Pre-Merge authority fails, the historical Release Producer upgrade hangs, the dry-run/parity artifacts were never produced, and DeepSec's candidate self-certification trust-boundary High remains uncompensated by a passing trusted authority.
