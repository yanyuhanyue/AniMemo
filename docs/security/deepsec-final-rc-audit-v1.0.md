# AniMemo v1.0 DeepSec Final RC Audit

Status: **FAIL / RELEASE BLOCKER**

Audited application candidate: `085657249c5d174e17dac7ff6dc0797f3179165a`

Tool: DeepSec `2.3.5` ([upstream repository](https://github.com/Unclecheng-li/DeepSec))

Completed: 2026-08-14 06:44, Asia/Shanghai

## Scope and source handling

Only tracked source and the explicit candidate delta were analyzed. The local DeepSec environment file was not read. Untracked files, working `.env` files, databases, backups, SSH material, production credentials, private runtime state, and production endpoints were excluded.

No Spear scan was run. No request targeted `animemo.cc`, the VPS, Cloudflare, R2, the real Bangumi application, localhost safety bypasses, or private network ranges.

Sanitized raw exports are retained outside the repository:

```text
C:\Users\admin\AppData\Local\Temp\animemo-deepsec-final-0856572\broad-findings.json
C:\Users\admin\AppData\Local\Temp\animemo-deepsec-final-0856572\exact-delta-findings.json
```

## Runs

| Run | Scope | Tool summary |
| --- | --- | --- |
| `20260813195454-71f63d8979a92b46` | Broad tracked-source analysis | 124 analyses, 114 report findings; Critical 0, High 2, Medium 65, High Bug 1, Bug 46 |
| `20260813222055-e85e738b3a20c3c5` | Exact delta `756bacd..0856572` | 6 changed files, 17 new analysis findings; report/export counters Critical 0, High 3, Medium 18, High Bug 1, Bug 4 |

The CLI overview, report counters, and JSON export use different counting units and include overlapping records. This report preserves the tool's numbers and triages by unique code-aware concern rather than adding duplicate rows together.

The final pass did not rerun the separate supply-chain command. DeepSec Supply Chain is therefore **NOT RUN**, not PASS.

## High-severity triage

### DS-RC1-001: candidate-controlled release and integrity authority

Disposition: **CONFIRMED HIGH, DUPLICATE GROUP, RC1**.

The broad and exact-delta runs report overlapping High findings in:

- `.github/workflows/release-gate.yml`
- `scripts/ci_gate_authority.py`
- `scripts/check_official_plugin_immutability.py`

The common issue is self-certification: candidate-controlled classifier, gate-authority, or package-identity code participates in deciding whether the same candidate is acceptable. Internal consistency checks are valuable, but they are not an independent trust anchor when both evidence and validator come from the candidate tree.

The repository has a trusted-main Pre-Merge workflow intended to compensate for this boundary. For exact candidate `0856572`, that authority failed because the reusable Release Gate from `main` lacked the required `dr-rehearsal` job and the compatibility path did not activate under the observed event. Because the compensation did not pass, these High findings cannot be dismissed as merely theoretical.

Required closure: execute classification, authority, and immutable package identity from protected trusted code, or have a protected authority independently recompute and verify candidate claims. Then obtain a passing exact-candidate Trusted Pre-Merge run.

### Artifact workflow permission report

Disposition: **FALSE POSITIVE**.

The exact-delta High Bug predicted that artifact upload/download would fail for lack of workflow permission. The final performance run uploaded, downloaded, and consumed the capacity and regression artifacts successfully. The observed execution contradicts the report.

### Updater backup retention report

Disposition: **CONFIRMED OPERATIONAL DEBT, DEFERRED RC2**.

The broad High Bug notes that pre-migration database backups have verification and freshness rules but no automatic retention/pruning policy. This can cause long-term disk pressure. It is not a current credential compromise, unsafe restore, or candidate data-loss path, and automatic deletion is intentionally outside the updater's current safety contract. Define an operator-visible inventory/retention policy before backup accumulation becomes an operational risk; do not silently auto-delete verified backups during this RC closure.

## Medium and Bug triage

| Finding group | Disposition | Triage |
| --- | --- | --- |
| Hardcoded Django/Fernet/staff values in CI | NOT APPLICABLE | Synthetic isolated fixtures; no production secret or production credential was present |
| Mutable GitHub Action/service image tags | CONFIRMED, DEFERRED RC2 | Real supply-chain hardening debt; pin to reviewed immutable SHAs/digests in a separate controlled change |
| CI trust/event/dispatch findings | DUPLICATE | Same root issue as DS-RC1-001; do not inflate the High count |
| Candidate SHA option/input handling | DEFERRED RC2 or DUPLICATE | Operator/CI script hardening; no demonstrated production request surface in this run |
| Fixed Compose namespace / ambient environment concerns | DEFERRED RC2 | Isolated rehearsal robustness debt; final authoritative workflows used scoped disposable resources successfully |
| Temporary fixture file permissions | DEFERRED RC2 | Synthetic workflow material, not production credentials; tighten private temp modes without widening RC scope |
| Artifact permission finding | FALSE POSITIVE | Artifact transfer succeeded in run `31745479369` |

No tracked production credential, private key, access token, Cloudflare/R2 secret, Bangumi App Secret, Resend key, or database dump credential was identified.

## Layer decision

| Requirement | Result | Reason |
| --- | --- | --- |
| DeepSec L1 / broad local analysis | PASS | Executed against tracked source; confirmed Critical 0 |
| DeepSec L2 / code-aware boundary analysis | **FAIL** | One grouped confirmed High in release/integrity authority |
| DeepSec L3 / exact candidate delta | **FAIL** | Reproduced the same self-certification High group on the final delta |
| DeepSec Supply Chain | NOT RUN | Separate command was not rerun in the final pass |
| Spear | NOT RUN | Forbidden production/private targets and tool safety boundary |

## Sanitized totals

Confirmed Critical: **0**.

Confirmed High: **1 grouped issue** represented by overlapping release/classifier/plugin authority findings.

False positives: **artifact permission High Bug plus synthetic-secret reports where the values are isolated fixtures**.

Deferred findings: **mutable action tags, backup retention, and bounded rehearsal/script hardening**.

These are triaged groups, not a relabeling of every duplicate JSON record as a unique vulnerability.

## Final security decision

DeepSec does not establish a Critical vulnerability in AniMemo v1.0. It does establish a release-blocking trust-boundary High: candidate-controlled logic certifies candidate release/integrity claims, while the intended trusted-main compensation failed for this exact candidate.

Security verdict: **FAIL / NOT READY** until the authority boundary is anchored to trusted code and a passing exact-candidate trusted authority run exists.
