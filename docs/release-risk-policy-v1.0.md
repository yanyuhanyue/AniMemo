# AniMemo v1.0 Release Risk Policy

This policy controls non-production validation selection. It does not authorize
an RC, image publication, production deployment, migration, backup, restore, or
promotion.

## Authority flow

```text
Development targeted tests
-> PR Fast selection-aware authority
-> explicit exact-HEAD/current-main Pre-Merge Full
-> squash merge
-> main lightweight verification
```

The personal repository does not use Merge Queue. `merge_group` remains
future-ready only. `pre-merge-authority` is valid only for the exact PR head and
base snapshot revalidated by the workflow. Required merge authority remains
`pr-fast-gate` plus `pre-merge-authority`; this policy does not permit a
workflow/job to be treated as an alternative authority.

## Levels

| Level | Definition | Typical selected validation |
| --- | --- | --- |
| LOW | Safe documentation or non-runtime repository metadata only | Documentation/metadata path; no product or release subset |
| STANDARD | Ordinary product implementation limited to a known subsystem | Affected product subsystem only |
| HIGH | Compatibility/security-sensitive API, plugin, database, dependency, media-write, or broad automation contract | Complete product matrix; Fresh Docker and stateful upgrade |
| CRITICAL | CI/release authority, release-image rehearsal, Updater, deployment, recovery, or first-run public trust boundary | Complete product matrix; Updater isolation, Fresh Docker, and stateful upgrade |

HIGH and CRITICAL both select the complete product correctness matrix. CRITICAL
adds the Updater failure/recovery subset because those changes can alter the
apply/switch/rollback authority itself.

`force_full` is an execution instruction, not a fifth risk level. Pre-Merge and
release dry-run callers may force every gate while preserving the inherent
LOW/STANDARD/HIGH/CRITICAL classification in evidence.

## Classifier coverage contract

The classifier applies the highest matching rule to each normalized path. A
specific boundary rule takes precedence over a broad product rule; for example,
`backend/site_config/urls.py` may match both API and first-run rules, but its
effective level is CRITICAL.

| Path family | Required level | Boundary covered |
| --- | --- | --- |
| `backend/site_config` public setup views, `*_views.py`, view modules, and route modules | CRITICAL | Public first-run status/setup entrypoint, CSRF/throttle boundary, and administrator creation |
| `src/App.*`, setup/first-run pages, and setup/first-run route modules or directories | CRITICAL | Uninitialized routing, `/setup` lock, and first-run UI boundary |
| Backend `urls.py`, `*_urls.py`, `routes.py`, `*_routes.py`, router/routing modules | At least HIGH | API route and endpoint contract; first-run site-config routes escalate to CRITICAL |
| `plugins/**` schema files or schema directories (`.json`, `.yaml`, `.yml`) | HIGH | Plugin SDK/manifest validation contract |
| Release/image/rehearse or release-image-rehearsal scripts and tests under release, scripts, or tests | CRITICAL | Exact image identity, labels, health, setup, and release rehearsal boundary |

The rules are family-based rather than a list of five filenames. New files in a
covered family must preserve the same level. An unmatched path fails closed to
CRITICAL instead of becoming an implicit LOW or STANDARD exception.

## LOW/STANDARD/HIGH/CRITICAL invalidators

| Current classification | Invalidators and required escalation |
| --- | --- |
| LOW | Any runtime/source/config/workflow/dependency/contract path, any unknown or empty input, or either side of a sensitive rename invalidates LOW. Sensitive operational and contract documentation is not LOW. |
| STANDARD | A change touching first-run/setup routes, API route modules, plugin schemas, release/image rehearsal, auth/security, database, dependencies, integration, media-write, deployment, or automation invalidates STANDARD and escalates to HIGH or CRITICAL according to the matching boundary. |
| HIGH | Any CI authority, release authority/image rehearsal, Updater, deployment, recovery/rollback, or first-run public trust-boundary path invalidates HIGH and escalates to CRITICAL. A HIGH result must still select Fresh Docker and stateful validation. |
| CRITICAL | Any changed input invalidates prior evidence reuse unless the complete identity proof below is recomputed. CRITICAL does not authorize production; it only selects the broadest non-production validation. |

Unknown and empty changed-file input is always CRITICAL. Both sides of a rename
are classified independently. A path may match multiple rules; the highest risk
level wins deterministically.

## Fail-closed authority rules

- Frozen API/Auth/Resource Identity/Plugin SDK/Integration/Release contracts
  are never treated as ordinary docs.
- A selected job that skips, fails, or is cancelled fails the aggregate
  authority.
- An unselected job that unexpectedly runs also fails authority; selection drift
  cannot silently become accepted behavior.
- Missing, malformed, contradictory, or future-unknown classifier output fails
  authority.
- Required `pre-merge-authority` remains bound to the exact candidate SHA and
  revalidated base snapshot.

## Reuse invalidators

Exact-SHA cross-run reuse is deferred until a proof binds all of:

- HEAD and Base SHA;
- trusted CI, Pre-Merge, Release Gate, and classifier revisions;
- relevant workflow inputs, test configuration, and dependency-lock identity;
- every consumed artifact digest, image digest, and provenance statement;
- the same risk classification and selected/unselected job result contract.

Any changed element invalidates previous evidence. HEAD-only reuse, branch-name
reuse, mutable-tag reuse, and unproven cross-gate artifact reuse are forbidden.
This change set introduces no reuse and does not weaken the required authority.

## Time-efficiency evidence policy

Cheap-fail-first may be recorded as PASS only when the workflow topology proves
that the required cheap checks complete before the expensive fan-out. A fast
changed-file classifier alone is insufficient. If static checks remain inside
product jobs or expensive release jobs can start in parallel before those checks
complete, the overall result is FAIL.

Before/after wall-clock claims require comparable candidate scope, test volume,
workflow revision, runner conditions, and authority stage definition. Without
those controls, the result is **INCONCLUSIVE**. Cache hit rate likewise requires
a trustworthy request/miss denominator; isolated primary-key hit messages are
not an aggregate rate.

Duplicate builds must be recorded qualitatively even when a numeric delta is not
comparable. Frontend, Fresh Docker API/Web, and Stateful Base/Current builds are
separate trust-boundary work until immutable digest/provenance reuse is proven.

## Release cadence

Full RC Readiness is for major milestones or high-risk infrastructure/release
changes, not every patch. LOW/STANDARD patches should use selected PR Fast gates
and one final Pre-Merge Full, targeting tens of minutes. HIGH/CRITICAL work keeps
the extended release subsets. Full sustained performance remains manual,
RC-oriented, or explicitly risk-triggered.

Implementation details and operator commands are in
[`release-gates.md`](release-gates.md).
