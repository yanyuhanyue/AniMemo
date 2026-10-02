# Native public confirmation

The Host confirmation uses a Win32 modal dialog. Its outer window fits the
current monitor work area, using a temporary per-thread DPI context restored
on close. Complete public scope stays in a read-only, vertically scrollable
edit control with word wrap; actions have a fixed bottom row. A and B have
distinct titles. An unusably small display or unavailable API rejects consent.

Initial focus is Cancel. Enter and Escape cancel even when Approve has focus.
After reaching the end of the scope, the operator can check the complete-scope
review box, then click Approve or deliberately focus it and press Space.
Scrolling or checking the review box alone does not approve anything.
Close, timeout, owner cancellation, or a changed exclusive Console identity
rejects approval. Existing scope, deadline, and one-attempt rules remain intact.
This dialog does not read a password or grant authority independently.

`local_candidate_development.py --result-only --result PATH` preserves exclusive
result creation, complete JSON, fsync, readback, cleanup, and exit code, while
skipping the final Console JSON copy. A source-bound operator launcher must
explicitly choose this option; no historical launcher was altered.

The previous 2026-10-01 live report records A approved, B cancelled, Runtime
NOT_RUN, holders released, and the fixed entry spent. This repair neither resets
that entry nor restores its live objects. New source SHA/tree requires a fresh
canonical material selection and execution binding. Any retry needs separately
approved, reviewable entry semantics; changing a label does not recover quota.

Verification is limited to synthetic public dialogs, layout calculations,
mocked report export, and guarded local regression tests. No Provider, bootstrap
key, private E materials, VM, Guest, installer, or real A/B is exercised.
