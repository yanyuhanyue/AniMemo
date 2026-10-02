# Single Runtime target handoff

This entry is DEV `PREPRODUCTION_ONLY`. It grants no Candidate acceptance,
Stable, Formal, publishing, or production qualification.

Inspect the public code scope with no private inputs, Provider, native prompt,
credential access, or VM calls:

```powershell
python -m scripts.local_candidate_development --runtime-target-handoff-scope
```

Runtime without `--execute` now returns `SCOPE_ONLY`; it does not build a
material-backed plan. The older three-profile planning flow is outside this
single Runtime handoff and still has its documented private preparation effects.

Execution requires `--execute --confirm-batch --runtime-offline-only
--runtime-target-handoff`, a fresh reviewed authorization label, the exact new
execution commit/tree, the original candidate/material/Q identities, and the
rebound selection's exact digest, execution inventory digest, guest inventory
digest and UTC expiry. It must run in the original operator's dedicated native
Windows console. Command flags and JSON data are requests, not consent.

Native **A** explains and separately confirms preparation: a fixed persistent
one-attempt task ledger, new private tool/material/source/work roots, a temporary
private copy of the existing bootstrap identity, and a private byte snapshot of
the hash-bound source VM files (including disks, historically about 7.5 GB).
The original VM, key, ACLs and prepared user materials remain unchanged.
Only after A does the entry reserve the fixed task attempt and construct the
Provider. Cancellation/failure does not recover the attempt.

Inside the same live Provider/material/source context, native **B** displays
the complete absolute clone VMX path, fresh session and plan digest, source
commit/tree/inventory, candidate/Q identity, snapshot and clone identities,
selection/guest inventory digests, material expiry and authorization deadline.
The native prompt has at most 60 seconds, clipped to the remaining wall and
monotonic lifetime. Confirmation validates the same held objects and path before
issuing the existing batch scope. The scope retains the live handoff and checks
it in subsequent batch construction, reservation and secret operations; serialized
target JSON and a different Provider cannot restore authority.

B covers only this fresh clone's single Runtime round. Existing one-capture,
role, W1 resource confirmation, argv/LZMA preflight, Linux root W2 signatures,
W3 Installer boundary, lease, clock, failure and STOP_AND_RETAIN checks remain
mandatory. Password input stays in the existing local native masked channel.
No password is accepted through chat, arguments, environment or saved files.

Rejecting B releases the newly owned temporary inputs and bootstrap key copy,
closes holders, and retains the new work root and spent entry. It does not delete
prepared user material or existing VM data. The outer cleanup stack closes an
owned session before the handoff even when cleanup diagnostics fail; failures
remain errors and are reported with fixed categories.

The original b2c59939 preparation receipt belongs to that exact source.
Host changes create a new execution commit/tree. They do not change the 237-file
Guest code projection or rebuild the original rc.3 product, but require a new
source-bound canonical selection and new selection/guest inventory digests.
Full execution inventory includes immutable producer extras and must be checked
in the approved actual holder flow; an unchanged digest is possible, not assumed.
Retained signature expiry cannot be extended by editing timestamps.

Use a clean checkout whose raw projected bytes equal Git blobs. On Windows,
disable automatic checkout CRLF conversion for this isolated checkout; do not
weaken the raw-byte check. Do not rerun the old preparation command with a new
source identity or edit the old receipt to imitate a new execution.

Before real execution, approve A's listed copies/private accesses, a resource
window that does not overlap InkWeft, and an explicit UTC deadline. Then confirm
B's actual target locally while the same context is alive. A and B have not
been approved by implementing or synthetically testing this entry.
