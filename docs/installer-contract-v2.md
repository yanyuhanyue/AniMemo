# AniMemo Installer Security Successor Contract v2

Status: FROZEN FOR v1.1 P1 SECURITY REPAIR

Predecessor: `docs/installer-contract-v1.md` at Git blob
`a6307b42928423cea3a2cf04db7836887fc818a0`.

This is a narrow security successor. It inherits Installer Contract v1 in full
and replaces only the bootstrap privilege and trust-lifecycle boundaries below.
It does not modify the v1 Release Authority, compatibility, deployment, backup,
rollback, or updater ownership contracts.

## 1. Authority and input classification

GitHub Immutable Release remains the single Release Authority. The install
portal, Official Mirror, Portable archive, checksums, bundle-contained verifier,
and bundle-contained roots are `UNTRUSTED_TRANSPORT_INPUT`. TUF authorizes only
GitHub/Sigstore trust-metadata succession and signer deauthorization; it cannot
mint an AniMemo Release.

Online Stage-0 uses independently installed `/usr/bin/gh` exactly `2.97.0` to
verify the exact tag and the exact protected `installer-materials.tar`. Offline
Stage-0 requires an operator or trusted image to provision the verifier and roots
independently of the Portable payload. Portable-only first trust is forbidden.

The online carrier may explicitly acquire only
`installer-materials.tar` from the fixed Official Mirror origin
`https://download.animemo.cc/yanyuhanyue/AniMemo/releases/download/<EXACT_TAG>/`.
It must verify the GitHub Immutable Release before that download, reject every
redirect and query, verify the mirror bytes with `gh release verify-asset`, copy
them into a root-owned candidate, and reverify both that candidate and its fixed
final path before extraction. Any verification failure removes paths created by
that invocation and leaves zero persistent AniMemo mutation. The receipt is a
transport completeness marker only and cannot satisfy either GitHub gate. This
flow is selected only by `--source official-mirror`; no automatic or error-driven
cross-source fallback exists. The exact command contract is frozen in
`docs/distribution-transports-v1.1.md`.

## 2. Verified bootstrap state machine

`UNTRUSTED_INPUT -> ACQUIRED -> AUTHORITY_VERIFIED -> PROTECTED_COPY_VERIFIED ->
PRIVILEGE_ALLOWED -> TRUST_PROVISIONING -> TRUST_PROVISIONED ->
INSTALLATION_AUTHORIZED -> INSTALLED`.

`PRIVILEGE_ALLOWED` requires a single-use `BOOTSTRAP_PRIVILEGE_GATE` capability
bound to repository, exact tag, protected archive identity, fixed authority root,
and the exact loaded core module bytes. The root launch uses a fixed protected
working directory, closed environment, `PYTHONSAFEPATH=1`, and Python `-P`; cwd
module shadowing is forbidden. No production skip, insecure, debug, environment,
or arbitrary-path bypass exists.

## 3. Trusted filesystem and TOCTOU boundary

Bootstrap authority root is `/var/lib/animemo/bootstrap-authority/v1`; trust
state root is `/var/lib/animemo/offline-trust/v2`. Both are fixed, root-owned,
not group/world writable, and reject symlinks, reparse substitutes, multi-link
files, path traversal, and identity drift. The copied archive is reverified after
the ownership transition and before extraction. Loaded Python source is rebound
byte-for-byte to the same protected archive before privilege is consumed.

## 4. Initial trust provisioning

The closed seven-file pretrust kit is an input only after the bootstrap
capability is valid. Its manifest binds the Linux verifier, GitHub/Sigstore
trusted roots, both TUF roots, and the closed TrustProfile. Provisioning stages a
new immutable generation, fsyncs files and directories, validates readback, and
atomically replaces the active record. Same-byte repeat is idempotent; different
bytes for an existing identity fail closed. Test fixtures carry explicit
`TEST-ONLY` identities and have no production authorization path.

## 5. Trust update, rollback, revocation, and crash policy

Only the current active TrustProfile may authorize exact successor `N+1`.
GitHub and Sigstore TUF root chains are verified sequentially with old-and-new
root threshold semantics; timestamp, snapshot, and targets versions must all
advance. Version skip beyond the fixed bound, stale parent, replay, downgrade,
wrong project, invalid threshold, expired metadata, or target mismatch fails
closed. A rotated or replaced material is `SUPERSEDED`, not automatically
`REVOKED`. Revoked signer identities must come from an explicit deauthorization
in a cryptographically verified TUF root successor.

Update commit order is: verify untrusted package; construct successor in a new
generation; fsync; readback; atomically replace the active record; fsync parent.
A crash before active replacement leaves the predecessor authoritative; a crash
after replacement must read back the complete successor. Normal rollback of
trust metadata is forbidden. Disaster recovery requires separate governance and
is not provided by this contract.

Offline status is limited to `AUTHENTIC_AS_OF_SIGNED_EVIDENCE`; future revocation
knowledge is `UNKNOWN_OFFLINE`. This contract never claims
`CURRENTLY_NOT_REVOKED` without sufficiently current verified metadata.

## 6. Closed failure taxonomy

Bootstrap failures use `BOOTSTRAP_*` and stop before AniMemo mutation. Trust
failures use `TRUST_*`, including unavailable/invalid authority, archive or
module drift, invalid state root, invalid bootstrap authorization, invalid
profile lineage, rollback/replay, cryptographic verification failure,
revocation inconsistency, staging failure, and readback failure. Failure codes
are stable and contain no credential, environment, raw attestation, or secret
payload values.

## 7. Production and qualification separation

Production requires official GitHub/Sigstore TUF roots, a Linux/amd64 verifier
built from the frozen module graph, and GitHub Release authority. Synthetic keys,
official-format fixtures, and namespace-isolated filesystem roots are allowed
only in qualification and cannot satisfy the production privilege gate. Live
Immutable Release acceptance remains deferred until the first separately
authorized RC.
