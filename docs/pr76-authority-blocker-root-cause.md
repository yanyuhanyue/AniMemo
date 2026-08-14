# PR #76 Authority Blocker Root-Cause Analysis

Evidence cutoff: `2026-08-14T07:37:19+08:00`

This report is a read-only analysis of existing repository state, GitHub Actions metadata, logs, annotations, and artifacts. No workflow was dispatched or rerun, no PR state was changed, and no production, release, or merge action was performed.

## 1. Frozen Baseline

| Field | Frozen value |
|---|---|
| Repository | `yanyuhanyue/AniMemo` |
| PR | `#76`, `OPEN`, `DRAFT`, `BLOCKED` |
| Branch | `work/integrated-final-rc-readiness-20260813` |
| Local HEAD | `5b95c3dc9a096e74023859f9541f72e8994edc84` |
| Remote branch HEAD | `5b95c3dc9a096e74023859f9541f72e8994edc84` |
| PR HEAD | `5b95c3dc9a096e74023859f9541f72e8994edc84` |
| `origin/main` / PR base | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| Local/remote parity | `PASS` |
| Pre-existing untracked file | `docs/DEEPSEC_LOCAL_USAGE.md` (not read, changed, staged, or deleted by this analysis) |

The exact current PR SHA has only two Actions runs:

- PR CI `31752022463`: `SUCCESS`
- PR Release Gate `31752022523`: `SUCCESS`

It has no `pre-merge-authority` commit status and no Trusted Pre-Merge or Release Producer run. Its combined commit-status endpoint contains zero status entries. Therefore the previously reported Trusted `FAIL` and Producer `CANCELLED` do **not** bind to the current exact SHA.

`5b95c3d` is a documentation-only commit relative to the last application candidate `0856572`; it changed four reports and no workflow or release script. This explains why the old defects remain relevant to remediation, but it does not convert old run evidence into exact-SHA authority evidence.

## 2. Authority Graph

```text
PR head SHA + PR base SHA
  |
  +-- Normal PR CI (pull_request, risk-selected, not force_full)
  |
  +-- Normal PR Release Gate (pull_request, risk-selected, not force_full)

Trusted current main workflow (manual workflow_dispatch)
  |
  +-- Pre-Merge snapshot
  |     - requires dispatch workflow from current main
  |     - binds PR number, exact PR head, and current PR base/main
  |
  +-- Reusable full CI(candidate=head, base=PR base, force_full=true)
  +-- Reusable full Release Gate(candidate=head, base=PR base, force_full=true)
  +-- Pre-Merge authority revalidates identity/freshness and both results

Candidate workflow revision (independent manual workflow_dispatch)
  |
  +-- Release Producer preflight
  |     - binds dispatch ref, explicit candidate, intended main, upgrade base
  |
  +-- Full CI(candidate, upgrade base, force_full=true)
  +-- Full Release Gate(candidate, upgrade base, force_full=true)
  +-- RC performance
  +-- Release authority
        |
        +-- read-only dry-run artifact production, or
        +-- immutable prerelease publish
```

Important boundaries:

- Normal PR checks are not substitutes for either authority workflow.
- Release Producer is not triggered by, and has no direct dependency on, the Trusted Pre-Merge status.
- Trusted Pre-Merge uses the PR base as its full-gate base (`8727aa9` in the relevant run).
- Release Producer uses the explicitly audited deployed upgrade base (`6452b3d` in the relevant runs).
- Release Producer concurrency is `animemo-release-producer` with `cancel-in-progress: false`.

## 3. Existing Run Matrix

### Current exact SHA

| Authority | Run ID | Event | Authority candidate | Base / intended main | Created / completed (UTC) | Attempt / actor | Jobs / artifacts | Result | Exact-SHA authority? |
|---|---:|---|---|---|---|---|---|---|---|
| Normal PR CI | `31752022463` | `pull_request` | `5b95c3d` | `8727aa9` | `2026-08-13 22:57:32` / `23:03:36` | `1` / `yanyuhanyue` | `14` / `0` | `SUCCESS` | Yes, normal PR channel only |
| Normal PR Release Gate | `31752022523` | `pull_request` | `5b95c3d` | `8727aa9` | `2026-08-13 22:57:32` / `22:59:36` | `1` / `yanyuhanyue` | `7` / `0` | `SUCCESS` | Yes, normal PR channel only |
| Trusted Pre-Merge | none | n/a | `5b95c3d` | `8727aa9` | n/a | n/a | n/a | `NOT RUN` | No |
| Release Producer | none | n/a | `5b95c3d` | `8727aa9` | n/a | n/a | n/a | `NOT RUN` | No |
| RC to Stable parity | none | n/a | `5b95c3d` | n/a | n/a | n/a | n/a | `NOT PROVEN` | No |

Both current PR runs have zero artifacts.

### Historical blocker evidence

The outer `head_sha` of a Trusted Pre-Merge run is the trusted `main` workflow revision. The authority candidate below comes from its validated dispatch input and `pre-merge-snapshot` output.

| Authority | Run ID | Event | Authority candidate | Base / intended main | Upgrade base | Created / completed (UTC) | Result |
|---|---:|---|---|---|---|---|---|
| Trusted Pre-Merge | `31739106618` | `workflow_dispatch` | `e64fedf` | `8727aa9` | n/a | `2026-08-13 20:05:20` / `20:12:15` | `FAILURE` |
| Trusted Pre-Merge | `31740134233` | `workflow_dispatch` | `9fe20ab` | `8727aa9` | n/a | `2026-08-13 20:17:43` / `20:24:06` | `FAILURE` |
| Trusted Pre-Merge | `31745482956` | `workflow_dispatch` | `0856572` | `8727aa9` | n/a | `2026-08-13 21:24:19` / `21:29:49` | `FAILURE` |
| Release Producer | `31740137975` | `workflow_dispatch` | `9fe20ab` | `8727aa9` | `6452b3d` | `2026-08-13 20:17:46` / `21:11:28` | `CANCELLED` |
| Release Producer | `31745485923` | `workflow_dispatch` | `0856572` | `8727aa9` | `6452b3d` | `2026-08-13 21:24:22` / `22:18:14` | `CANCELLED` |

All listed runs were attempt 1 and were initiated by `yanyuhanyue`. The latest Trusted run used reusable CI and Release Gate definitions from trusted `main@8727aa9`; the latest Producer used the candidate workflow definitions from `0856572`.

## 4. Trusted Pre-Merge Failure

### Last allowed attempt

| Field | Evidence |
|---|---|
| Run | `31745482956` |
| Bound candidate | `085657249c5d174e17dac7ff6dc0797f3179165a` |
| Bound base | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| Trusted workflow revision | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| First failing job | `full-release-gate / release-gate-authority` (`94599407860`) |
| First failing step | `Require selected jobs to succeed and unselected jobs to skip` |
| Command | `python scripts/ci_gate_authority.py --workflow release --event-name "workflow_dispatch"` |
| Exit code | `2` |
| First/root error | `{"code": "ci_gate_authority_failed", "detail": "NEEDS_JSON has missing keys: dr-rehearsal", "status": "FAIL"}` |
| Downstream failure | `pre-merge-authority` propagated `FULL_RELEASE_GATE_RESULT=failure` |

The two immediately preceding relevant runs, `31740134233` and `31739106618`, failed in the same job and step with the same exit code and exact missing-key error. The selected updater, Docker, and stateful jobs succeeded. This is a **same, deterministic failure signature**, not runner, network, dependency-install, permission, or test instability.

### Root mechanism

1. The trusted caller and reusable Release Gate revision are protected `main@8727aa9`.
2. That revision's `.github/workflows/release-gate.yml` has no `dr-rehearsal` job, and `release-gate-authority.needs` omits it.
3. The called workflow checks out the candidate before authority validation, so candidate `scripts/ci_gate_authority.py` supplies the validation schema.
4. The candidate schema requires `dr-rehearsal` in `RELEASE_JOB_GATES`.
5. Candidate `0856572` added a legacy-main schema without `dr-rehearsal`, but enables it only when `event_name == "workflow_call"` and the trusted workflow ref matches.
6. A reusable workflow retains the caller's event. The authority command therefore observes `workflow_dispatch`, while only the classifier is given a synthesized `GITHUB_EVENT_NAME=workflow_call` for `force_full` classification.
7. The compatibility path is bypassed and exact-key validation deterministically fails.

Classification: **AUTHORITY failure**, specifically an **RC0 release-authority design/contract defect**. The trusted workflow job graph and the candidate validator schema are an unversioned cross-revision contract, and the compatibility decision relies on the wrong event semantic.

The later `pre-merge-authority` failure is downstream propagation, not an independent root cause.

Current exact-SHA status: **NOT RUN / NOT PROVEN**, not `FAIL`. The proven failure belongs to stale candidate `0856572` and earlier application candidates.

## 5. Release Producer Cancellation

### Last allowed attempt

| Field | Evidence |
|---|---|
| Run | `31745485923` |
| Candidate | `085657249c5d174e17dac7ff6dc0797f3179165a` |
| Intended main | `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` |
| Upgrade base | `6452b3dbfff39529c49c2bc69ede1f3d76236eee` |
| Directly cancelled job | `full-release-gate / stateful-upgrade` (`94598984553`) |
| Job annotation | `The job has exceeded the maximum execution time of 40m0s` |
| Final step error | `The operation was canceled.` |
| Release authority | `ReleaseAuthorityError: {"full-release-gate": "cancelled"}` |
| Dry-run producer | `read-only-release-dry-run`: `SKIPPED` |
| Publish producer | `publish-immutable-prerelease`: `SKIPPED` |

The preceding allowed attempt, run `31740137975`, has the same direct mechanism: stateful job `94581443835` exceeded its then-configured `30m0s` maximum. Increasing the job limit from 30 to 40 minutes did not change the outcome.

The cancellation mechanism is therefore **PROVEN: job-level timeout**. It was not caused by `cancel-in-progress`, superseding workflow execution, matrix fail-fast, environment approval, permission denial, manual whole-run cancellation, or Trusted Pre-Merge failure.

### Narrowest proven internal boundary

Existing logs prove that the stateful script completed Base image build, persistent-service startup, migration, and bootstrap. The Base API emitted Gunicorn startup, listening, and worker-boot messages. No health success and no next-stage marker appeared.

In `scripts/stateful-upgrade-gate.sh`, that narrows the stall to this boundary:

```text
line 248: compose "$BASE_ROOT" up -d --no-deps api
line 249: if ! wait_for_api "$BASE_ROOT" "BASELINE"; then
...
line 254: echo "== Seed representative persistent Base state =="
```

The logs do **not** distinguish whether line 248 failed to return or `wait_for_api` entered and then blocked in an inner Docker command. The specific internal blocking command is therefore **UNKNOWN**. Treating `compose up` or `docker exec` as proven would exceed the evidence.

### Proven causal chain

```text
stateful-upgrade reaches its job-level maximum execution time
  -> reusable full-release-gate result becomes cancelled
  -> release-gate-authority fails closed because selected stateful-upgrade is cancelled
  -> top-level release-authority receives full-release-gate=cancelled
  -> release_authority.py raises ReleaseAuthorityError
  -> read-only-release-dry-run and publish-immutable-prerelease are skipped
  -> Release Producer run concludes cancelled
```

Causal relationship to Trusted Pre-Merge: **NO**. They are independent dispatch workflows, use different reusable workflow revisions and different base semantics, and do not share a dependency edge. The Trusted run's stateful job against PR base `8727aa9` succeeded in about two minutes, while the Producer stateful job against deployed upgrade base `6452b3d` timed out.

Current exact-SHA status: **NOT RUN / NOT PROVEN**, not `CANCELLED`.

## 6. RC to Stable Parity

The current repository contract requires promotion to reuse the RC identity rather than rebuild it:

- RC commit equals Stable commit.
- RC API image digest equals Stable API image digest.
- RC Web image digest equals Stable Web image digest.
- Deployment data remains equal.
- The deployment-contract artifact identity remains equal.
- Stable is derived with `promote-manifest`; promotion consumes the existing RC digests and artifacts.

RC and Stable manifest bytes are intentionally not identical because version, channel, `promotedFrom`, timestamps, and provenance workflow fields differ. Checksum files are regenerated over the appropriate manifest and deployment contract; byte-for-byte equality of the two checksum files is not the repository's parity contract.

For historical Producer run `31745485923`, only these five performance artifacts exist:

- `performance-regression-gate`
- `performance-resource-load`
- `performance-long-operation-capacity`
- `performance-frontend`
- `performance-backend`

There is no release dry-run artifact, API/Web release artifact, release manifest, deployment contract, checksums, provenance plan, RC metadata, or Stable simulation. The producer job that would create those files was skipped.

| Evidence | Current exact SHA `5b95c3d` |
|---|---|
| API digest | `NOT PROVEN` |
| Web digest | `NOT PROVEN` |
| RC manifest | `NOT PROVEN` |
| Stable manifest | `NOT PROVEN` |
| Deployment contract identity | `NOT PROVEN` |
| Provenance inputs | `NOT PROVEN` |
| Overall RC to Stable parity | **`NOT PROVEN`** |

## 7. Historical Successful Comparison

### Trusted Pre-Merge

The nearest known successful Trusted Pre-Merge is run `31663655749` (`SUCCESS`, 2026-08-13), whose trusted workflow revision was `0f2a25d800fe7362fd2293773fd69c0ef39ba85f`. Its `release-gate-authority` and final `pre-merge-authority` jobs succeeded. It predates the present `dr-rehearsal` schema drift and therefore does not prove compatibility of `main@8727aa9` with the candidate validator.

### Release Producer and parity dry-run

Run `31588385877` is a genuine successful historical read-only Release Producer for commit `179688c3478555f52ee66c78bfec103e1766047d`. Artifact `release-dry-run-v1.0.0-rc.1` proves the older contract's dry-run promotion:

- RC `v1.0.0-rc.1` to Stable `v1.0.0`
- identical commit `179688c3478555f52ee66c78bfec103e1766047d`
- identical API digest `sha256:678f80bc9de85a4ad8b4f54647d2f55f10dd85c216f06f3411d97d1dd3847be6`
- identical Web digest `sha256:cc9aae200ff359a2d8b30fe712c08148e6a987d940d801a42279307b865667ca`
- Stable `promotedFrom=v1.0.0-rc.1`

That artifact also contains an unsigned provenance plan. It predates the current deployment-contract requirement: it contains no `deployment-contract.json`, no manifest `.deployment` section, and its checksums cover only the RC manifest. It is therefore informative historical evidence, not a successful equivalent of the current stronger contract.

No single historical exact-candidate chain was found that combines successful Trusted Pre-Merge, successful Release Producer, and current-format RC to Stable parity. Historical success cannot be transferred to `5b95c3d`.

## 8. Root Cause

### Primary exact-SHA classification

**STALE**: the cited Trusted `FAIL` and Producer `CANCELLED` belong to `0856572` and earlier candidates, not current exact SHA `5b95c3d`. Current Trusted Pre-Merge, Release Producer, and parity evidence remain **NOT RUN / NOT PROVEN**.

### Proven underlying blockers

1. **RC0 - RELEASE AUTHORITY DESIGN/CONTRACT DEFECT**

   Trusted `main@8727aa9` exposes a legacy Release Gate job graph, while the candidate validator expects the newer graph. Its legacy compatibility path tests `workflow_call`, but the observed reusable-workflow event is `workflow_dispatch`. This deterministically prevents Trusted authority from completing.

2. **RC1 - RELEASE BLOCKER: PRODUCER STATEFUL TIMEOUT**

   Two Producer attempts reached their 30- and 40-minute stateful job limits at the same script phase. The timeout and downstream cancellation chain are proven. The exact inner blocking command remains unknown from existing logs.

These are separate causal chains. Neither normal PR PASS result resolves them, and neither old failure result certifies current exact SHA.

## 9. Required Resolution

Decision: **FIX REQUIRED - NEW SHA**.

### Authority contract change

Required change:

- Make the trusted reusable workflow job graph and candidate validator an explicit, versioned contract.
- At minimum, identify the trusted legacy Pre-Merge invocation using reliable caller/workflow identity and its observed `workflow_dispatch` semantics, rather than requiring `event_name == workflow_call`.
- Preserve exact-key and selected/skipped fail-closed validation for every supported graph version.

Expected files include:

- `.github/workflows/release-gate.yml`
- `scripts/ci_gate_authority.py`
- `scripts/tests/test_ci_gate_authority.py`
- `scripts/tests/test_ci_authority_workflows.py`
- `scripts/tests/test_release_workflows.py`

Targeted validation must cover the exact combination that failed: trusted Pre-Merge caller, outer `workflow_dispatch`, `force_full=true`, legacy main graph without `dr-rehearsal`, and current graph with `dr-rehearsal`.

### Producer stateful-gate change

Required change:

- Add before/after phase evidence around Base API `compose up` and every health-loop subprocess.
- Bound each potentially blocking Docker/Compose call independently; do not rely only on the shell loop deadline or GitHub job timeout.
- On timeout, capture `compose ps`, container inspect/state, API logs, and the precise command that exceeded its bound.
- Reproduce and resolve the `6452b3d -> candidate` isolated upgrade path in non-production validation.

Expected files include `scripts/stateful-upgrade-gate.sh`, the Release Gate workflow timeout/contract tests, and focused stateful-upgrade test fixtures. The final code fix should follow the newly proven inner cause; this report does not guess it.

### Why a new SHA is mandatory

- The authority mismatch is repository workflow/script behavior.
- The Producer path needs bounded execution and enough diagnostics to identify and eliminate its internal stall.
- `5b95c3d` changed reports only, so neither underlying implementation changed after `0856572`.
- Repeating the same inputs on unchanged behavior would not create trustworthy evidence and could only repeat or obscure deterministic blockers.

After a new candidate and current base are frozen, fresh Trusted Pre-Merge and read-only Release Producer authority runs are required. They require explicit user authorization; this analysis does not authorize or execute them.

## 10. Retry Policy

| Policy | Status |
|---|---|
| Automatic retry budget | `EXHAUSTED` |
| Same-SHA manual retry justified | `NO` |
| Third/additional authority attempt in this analysis | `NOT AUTHORIZED / NOT EXECUTED` |
| Rerun failed or cancelled jobs | `NOT EXECUTED` |

## 11. Production Safety

| Action | Status |
|---|---|
| Production access or mutation | `NOT RUN` |
| Release/tag/package/publish | `NOT RUN` |
| Stable promotion | `NOT RUN` |
| PR Ready/Draft change | `NOT RUN` |
| Merge | `NOT RUN` |
| Workflow dispatch/rerun | `NOT RUN` |
| Security scanner | `NOT RUN` |
| Pre-existing untracked file touched | `NO` |

## 12. Final Decision

```text
PR #76                         OPEN / DRAFT / BLOCKED
Candidate SHA                  5b95c3dc9a096e74023859f9541f72e8994edc84
Base SHA                       8727aa97dc092d12e4a4abb15b85ce1f46d1020d
Normal PR CI                   PASS
Normal PR Release Gate         PASS
Trusted Pre-Merge              NOT RUN / NOT PROVEN for current SHA
Release Producer               NOT RUN / NOT PROVEN for current SHA
RC -> Stable parity            NOT PROVEN

Trusted failure root cause     PROVEN for stale 0856572 run
Producer cancel cause          PROVEN job timeout; inner command UNKNOWN
Same causal chain              NO

Code change required           YES
New SHA required               YES
Same-SHA manual retry valid    NO
Third attempt executed         NO

Production touched             NO
PR merged                      NO
Pre-existing untracked touched NO

FINAL:
BLOCKER ROOT CAUSE PROVEN
AWAITING USER DECISION
```

This is not `READY TO MERGE`. Exact-SHA authority and current-contract parity remain unproven.

## Remediation

Remediation was implemented and validated in the local working tree on top of
the frozen candidate `5b95c3dc9a096e74023859f9541f72e8994edc84`. That old SHA remains blocked;
this remediation tree becomes a new candidate only after the pre-commit matrix
passes and the intentional remediation commit is created.

### RC0 - Versioned Release Gate graph contract

The Release Gate authority boundary now uses an explicit versioned job-graph
contract instead of inferring compatibility from the outer event name:

- Current callers explicitly select `animemo.release-gate.jobs/v2`; its exact
  job graph includes `dr-rehearsal`.
- Historical `animemo.release-gate.jobs/v1` cannot be selected explicitly by a
  current caller. It is accepted only when the repository, trusted caller
  workflow ref, workflow SHA, and caller SHA all exactly authenticate the frozen
  legacy `main@8727aa97dc092d12e4a4abb15b85ce1f46d1020d` invocation.
- Missing, unknown, mismatched, or downgrade contract states fail closed.
- Exact NEEDS keys and selected-success / unselected-skipped semantics remain
  mandatory for both supported graphs.

RC0 targeted authority/workflow validation: `62/62 PASS`.
Classifier/ref regression validation: `22/22 PASS`.
Pre-merge/first-run validation: `9/9 PASS`.

RC0 remediation status: **PASS**.

### RC1 - Stateful-upgrade bounded instrumentation

`scripts/stateful-upgrade-gate.sh` now emits machine-readable phase, command,
and diagnostic markers. Docker Compose build, start, run, exec, ps, replace,
restart, cleanup, health probe, inspect, and diagnostic calls are independently
bounded with GNU `timeout --foreground --kill-after`. Timeout exits `124` and
`137` are classified as timeouts; ordinary command failures retain their
original exit code. Diagnostics collect bounded Compose status, detailed API
container `.State`, and tailed logs without replacing the root failure.

The health contract was not relaxed: the probe remains container-local
`127.0.0.1:8000/health/` with `Host: ci.example.test`,
`X-Forwarded-Proto: https`, HTTP 200, and JSON `status=ok`. Restarting containers
continue polling; exited containers fail closed.

The fake-Docker harness exercises 13 bounded failure, recovery, and command
contract scenarios: compose timeout, forced kill after ignored TERM,
non-timeout exit-code preservation, explicit legacy bootstrap override, health
recovery, hanging health command, permanently unhealthy API, hanging inspect,
exited API, restarting API recovery, hanging diagnostics, restart failure, and
the complete success path. Result: `13/13 PASS`.

Deployment/updater static contract validation: `15/15 PASS`.

RC1 instrumentation status: **PASS**.

### Local container environment

The real rehearsal used an isolated local Windows Docker Desktop environment:

- WSL `2.7.11.0`, default WSL version `2`.
- Docker Desktop `4.86.0 (236216)` using the Linux/amd64 WSL 2 backend.
- Docker Engine `29.7.2`.
- Docker Compose `v5.3.1`.
- Docker application and WSL data roots under `E:\番剧记录\Docker`.
- No production, shared VPS, Cloudflare, release workflow, or authority workflow
  was accessed.

Windows Git Bash cannot apply the Linux UID/GID required by the private runtime
bind mount. A repository-external, narrow local helper created the rehearsal
directory as `10001:10001` mode `0700`; an independent container running as UID
and GID `10001` then proved read, write, and execute access. This host adaptation
was not added to the repository.

### RC1 - Proven inner blocker and root fix

The first real Docker rehearsal was project
`animemo-rc1-20260814-093655`, using historical base
`6452b3dbfff39529c49c2bc69ede1f3d76236eee` and the current remediation working
tree. Its retained log is
`E:\番剧记录\Docker\rehearsal\rc1-20260814-093655.log`.

All preceding phases passed: Base Compose validation, Base API image build,
PostgreSQL/Redis startup, and Base migration. The first product blocker was:

```text
BLOCKING PHASE:     base_bootstrap
BLOCKING COMMAND:   docker compose run --rm --no-deps bootstrap
BOUND:              300 seconds
EXIT / TIMEOUT:     timeout, exit 124
CONTAINER STATE:    running, no restart, no OOM, one-off with AutoRemove
REPRODUCIBLE:       YES
```

The one-off completed migrations, `sync_official_plugins`, and `collectstatic`,
then started Gunicorn, listened on `0.0.0.0:8000`, and booted two workers. It
remained alive and idle instead of returning success to the gate.

The exact repository cause is the cross-revision Compose merge:

1. Historical `6452b3d` has no `bootstrap` service in
   `deploy/docker-compose.yml`.
2. `deploy/docker-compose.upgrade-gate.yml` creates the compatibility
   `bootstrap` service but intentionally supplies no `command` so current source
   trees retain their own bootstrap definition.
3. For the historical source tree, the resulting service therefore inherited
   the historical API image default command, whose final operation is
   long-running Gunicorn.

The minimum fix is confined to the historical Base invocation in
`scripts/stateful-upgrade-gate.sh`: it explicitly overrides the Base one-off to
run `sync_official_plugins`, then `collectstatic --noinput`, and exit. Migration
remains the preceding explicit phase. The Current invocation still uses the
current source tree's `bootstrap_animemo` command. No production Compose
architecture, health contract, or timeout was weakened or increased.

Regression coverage proves that the first bootstrap call contains only the
finite historical bootstrap operations, contains neither Gunicorn nor a second
migration, and that the Current bootstrap continues to inherit the current
source-tree command. The stateful test harness also now selects Git Bash ahead
of the WSL launcher on Windows and remains portable to POSIX runners without
requiring `cygpath`.

RC1 inner blocker: **PROVEN**.
RC1 root-fix status: **PASS**.

### Successful isolated rerun

An intermediate rerun, project `animemo-rc1-20260814-103530`, proved the root
fix by passing Base bootstrap and Base API health, then failed at
`base_state_seed` because Git for Windows rewrote the container path
`/app/ci-scripts/...` into `C:/Program Files/Git/app/ci-scripts/...`. That was a
local MSYS argument-conversion issue, not an AniMemo runtime blocker. A scoped
process-only exclusion for `/app/ci-scripts` and `/app/ci-meta` was validated and
used without adding Windows-specific repository code.

The final real rehearsal was project `animemo-rc1-20260814-103813`; its retained
log is `E:\番剧记录\Docker\rehearsal\rc1-20260814-103813.log`.

```text
6452b3d Base build/start/migration/bootstrap/health   PASS
Representative Base state seed and verification      PASS
Current build/migration/bootstrap                     PASS
API-only replacement and health                       PASS
PostgreSQL and Redis identity retention               PASS
Migration check and plan                              PASS
Persistent data verification                          PASS
API restart, recovery, and re-verification             PASS
Final stateful gate                                    PASS
Exit code                                              0
Duration                                               169 seconds
```

Cleanup removed only that isolated project's containers and network. No global
Docker prune or unrelated resource removal was used.

### Local validation evidence

- Full `scripts/tests` discovery: `236 PASS`, `2 SKIPPED`, `0 FAIL`.
- Release authority / manifest / promotion suites: `28/28 PASS`.
- RC0 authority/workflow suites: `62/62 PASS`.
- Classifier/ref suites: `22/22 PASS`.
- Pre-merge/first-run suites: `9/9 PASS`.
- Python `compileall`: `PASS`.
- All tracked Bash scripts syntax check: `PASS`.
- Workflow YAML parse: `7/7 PASS`.
- `git diff --check`: `PASS`.

The full suite printed an existing dependency-lock consistency diagnostic for
`Django==5.2.16` versus `Django==5.2.17`; it did not fail the suite and was not
changed because dependency refresh is unrelated to RC0/RC1.

### Remediation boundary

```text
RC0 authority contract         PASS
RC1 instrumentation            PASS
RC1 inner blocker              PROVEN
RC1 root fix                   PASS
6452b3d -> candidate upgrade   PASS (169 seconds)
New SHA                        PENDING INTENTIONAL REMEDIATION COMMIT
Push                           NOT RUN
Trusted Pre-Merge              NOT RUN
Release Producer               NOT RUN
Production                     NOT RUN
PR Ready                       NO
Merge                          NO
DEEPSEC_LOCAL_USAGE touched    NO

FINAL:
LOCAL PRE-COMMIT MATRIX PASS
OLD SHA REMAINS BLOCKED
```

## Release rehearsal trusted proxy remediation

The next exact-SHA authority attempt used candidate
`374c034ad6860db36286de9b33b80d02e82cae15`. That SHA remains permanently
blocked. Its completed authority evidence is:

- Normal PR CI run `31764881646`: `PASS`.
- Normal PR Release Gate run `31764881640`: `PASS`.
- Trusted Pre-Merge run `31765568471`: `PASS`, with trusted caller/workflow
  revision `8727aa97dc092d12e4a4abb15b85ce1f46d1020d` and candidate input
  `374c034ad6860db36286de9b33b80d02e82cae15`.
- The first Producer run `31766043898` failed because the initial v1 release
  line requires `target_version_override=v1.0.0`.
- The corrected same-SHA Producer run `31766381984` passed release preflight,
  full CI, full Release Gate, stateful upgrade, RC performance, and release
  authority. Its stateful-upgrade gate passed in 110 seconds.
- The first candidate failure was job `read-only-release-dry-run`, step
  `Start and accept the exact locally built API and Web images`, with
  `django.core.exceptions.ImproperlyConfigured: TRUSTED_PROXY_IPS` rejecting
  the overly broad `172.16.0.0/12` network.

This second Producer failure is a candidate release-rehearsal contract defect,
not an authority invocation defect. A same-SHA retry is not valid and no result
from `374c034...` transfers to the remediation candidate.

### Proven proxy topology and source locus

The exact-image rehearsal request path is:

```text
Host acceptance client -> Web/Nginx container -> API/Django container
```

The Web service is the direct reverse-proxy peer seen by Django. The isolated
Compose network is created per rehearsal project and Docker assigns its subnet
and service addresses at runtime, so another fixed private CIDR would not be a
portable correction. The local exact-image run observed the Web peer as
`172.18.0.5`, making the minimum trust representation for that run
`172.18.0.5/32`.

The deterministic defect was in `scripts/rehearse-release-images.sh`, which
previously wrote `TRUSTED_PROXY_IPS=172.16.0.0/12`. The production-style
validator in `backend/config/settings.py` correctly rejected that broad private
network and remains unchanged.

### Minimum root fix

The rehearsal now:

1. uses the valid bootstrap value `127.0.0.1/32` while migrations, bootstrap,
   and the initial API/Web startup complete;
2. inspects the running Web container on the project-scoped network;
3. validates its assigned address as IPv4 and converts exactly that address to
   a `/32`;
4. writes that exact value to the isolated rehearsal environment;
5. force-recreates only the API service so Django loads the exact Web proxy
   source; and
6. verifies the first-run `AdminAuditLog` client IP differs from the Web peer,
   proving Django accepted the client address forwarded by the trusted proxy.

No Compose architecture, backend trust-width rule, timeout, production
configuration, or frozen v1 contract was changed.

### Regression and real Docker evidence

- Targeted release-rehearsal tests: `2 PASS`.
- Targeted backend security tests: `5 PASS`.
- Full scripts suite: `237 PASS`, `2 SKIPPED`.
- Full backend suite: `631 PASS`, `36 SKIPPED`.
- Frontend tests: `171 PASS`.
- ESLint, Ruff fatal-rule scan, Python compileall, Django system check,
  migration drift check, dependency lock check, workflow YAML parse, tracked
  Bash syntax, and `git diff --check`: `PASS`.
- Isolated exact API/Web image rehearsal: `PASS` in 41 seconds, with Web proxy
  source `172.18.0.5/32` and first-run setup smoke `PASS`.
- Isolated historical stateful upgrade
  `6452b3dbfff39529c49c2bc69ede1f3d76236eee` to the remediation working tree:
  `PASS` in 157 seconds.

All dedicated Compose projects, containers, networks, and named volumes were
removed project-specifically. No global Docker cleanup, production access,
publication, or promotion occurred.

### Candidate identity boundary

The new candidate is the single normal descendant commit containing this
remediation section and the three narrow source/test changes. A Git commit
cannot embed its own final hash without changing that hash, so the authoritative
40-character candidate SHA is recorded immediately after commit creation in the
exact-SHA authority evidence and final result matrix.

```text
Previous candidate 374c034   BLOCKED
Proxy topology               PROVEN
Required trust               runtime Web peer /32
Backend validator weakened   NO
Minimum root fix             PASS
Local exact-image rehearsal  PASS (41 seconds)
Historical upgrade           PASS (157 seconds)
New candidate SHA            ENCLOSING REMEDIATION COMMIT
Production                   NOT RUN
PR Ready                     NO
Merge                        NO
```
