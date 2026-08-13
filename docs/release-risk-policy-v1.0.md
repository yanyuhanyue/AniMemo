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
base snapshot revalidated by the workflow.

## Levels

| Level | Definition | PR Fast Release subset |
| --- | --- | --- |
| LOW | Safe docs or non-runtime metadata | None |
| STANDARD | Ordinary product implementation limited to known subsystems | None |
| HIGH | Compatibility/security-sensitive product contract, database, dependency, media-write, or broad automation change | Fresh Docker and stateful upgrade |
| CRITICAL | CI/release authority, Updater, deployment, recovery, or first-run trust boundary | Updater isolated failure/recovery, Fresh Docker, and stateful upgrade |

HIGH and CRITICAL both select the complete product correctness matrix. CRITICAL
adds the Updater failure/recovery subset because those changes can alter the
apply/switch/rollback authority itself.

`force_full` is an execution instruction, not a fifth risk level. Pre-Merge and
release dry-run callers force every gate while preserving the inherent
LOW/STANDARD/HIGH/CRITICAL classification in evidence.

## Fail-closed rules

- Unknown or empty changed-file input is CRITICAL.
- Both sides of a rename are classified independently.
- Frozen API/Auth/Resource Identity/Plugin SDK/Integration/Release contracts
  are never treated as ordinary docs.
- A selected job that skips, fails, or is cancelled fails the aggregate
  authority.
- An unselected job that unexpectedly runs also fails authority; selection drift
  cannot silently become accepted behavior.
- Missing, malformed, contradictory, or future-unknown classifier output fails
  authority.

## Reuse invalidators

Exact-SHA cross-run reuse is deferred until a proof binds all of:

- HEAD and Base SHA;
- trusted workflow, Release Gate, and classifier revisions;
- relevant test configuration and dependency-lock identity;
- every consumed artifact digest and provenance statement.

Any changed element invalidates previous evidence. HEAD-only reuse is forbidden.

## Release cadence

Full RC Readiness is for major milestones or high-risk infrastructure/release
changes, not every patch. LOW/STANDARD patches should use selected PR Fast gates
and one final Pre-Merge Full, targeting tens of minutes. HIGH/CRITICAL work keeps
the extended release subsets. Full sustained performance remains manual,
RC-oriented, or explicitly risk-triggered.

Implementation details and operator commands are in
[`release-gates.md`](release-gates.md).
