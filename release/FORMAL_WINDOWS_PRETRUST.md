# Formal Windows verifier pretrust contract

The release workflow builds two verifier binaries from the same frozen Go
source checkout:

- `offline-release-verifier` with `CGO_ENABLED=0 GOOS=linux GOARCH=amd64`;
- `formal-release-verifier.exe` with
  `CGO_ENABLED=0 GOOS=windows GOARCH=amd64`.

`scripts/formal_windows_pretrust.py build` combines those binaries with the
already validated GitHub and Sigstore trust roots into one closed, dual-platform
kit. The Windows host verifier and Linux guest verifier have independent byte
identities. The profile binds both identities, the source Linux trust-profile
identity and every trusted/TUF root identity. The manifest binds the exact file
set, sizes, modes and aggregate kit identity.

The kit is embedded under
`release/release_attestation_verifier/formal-windows-amd64-pretrust-v1/` in the
single authoritative `installer-materials.tar`. The prepublication-materials
contract records all resulting identities. A Formal consumer derives its
expected binding from the already verified Candidate installer-material bytes
with `inspect_formal_windows_pretrust_in_installer_materials`; operator-provided
paths or profile fields are not authenticity anchors.

On Windows, private authority roots remain restricted to the current worker,
LocalSystem and Builtin Administrators. Their full fixed-volume path chain and
declared files are held by file identity without delete/write sharing for the
execution lifetime. TrustedInstaller write access is accepted only by
`hold_windows_audited_system_tool_source`, only for a direct System32 PE with a
pinned digest. `hold_windows_system_tool_private_snapshot` then O_EXCL-copies,
rehashes and PE-validates that source into the strict private snapshot; Formal
executes the held private copy, never the System32 source.

No Formal consumer may rebuild either verifier or trust kit in the field.
