# Separately approved single Runtime retry

The public checkout contains reusable validation logic and synthetic tests. It
contains no operator task IDs, historical receipt identities, private paths or
approved execution window. Its retry policy reports OPERATOR_BINDING_REQUIRED;
importing or testing it grants no Runtime capability.

The reviewed private operator overlay supplies an exact prior entry ID, a new
fixed entry/permit, an exclusive entry root, and the prior receipt's path, byte
length and SHA-256. These form one indivisible anti-replay binding. Do not replace
individual values with convenient placeholders, use a different receipt, reset
spent entries, or derive a new quota from a changed source/session label.

The retry selector requires the complete offline target handoff, confirmation,
execution and result-only scope. Receipt checks must establish B cancellation,
no credential session, Runtime NOT_RUN, closed/spent entry and released holders.
The receipt supplies historical evidence; it cannot restore a live holder,
consent, credential capability or an expired approval.

A fresh request needs separate approval for the exact source/material binding,
preparation effects, new target and bounded UTC window. Native A and B are still
required in the same held context. The effective expiry cannot exceed material
expiry, and a new window never extends an earlier approval automatically.

A covers private preparation and source VM snapshot effects; B covers the exact
new clone and approved single Runtime round. Existing one-capture and W1/W2/W3
gates remain mandatory. Cancellation, failure or uncertain cleanup spends the
attempt and follows STOP_AND_RETAIN. No automatic retry, credential/ACL recovery,
Candidate acceptance or publishing is granted.

See [cloud/local development](cloud-local-development.md) for the private-overlay
contract and source-freeze boundary. The historical operator binding is retained
only in the authorized private handoff; it is not a public execution recipe.
