# Plugin SDK v2

Every plugin uses `manifest.json` with `schemaVersion: 2` and `sdkApi: 2`.
Frontend entries export `createPlugin(host)` and return `routes`, `navigation` and
`dispose()`. Routes must declare `area` and `access` (`public`, `auth`, or `staff`).
Permissions use the explicit Staff roles `reviewer`, `user_manager`, `operator`,
and `administrator`.

Backend runtimes may optionally declare provider-neutral Integration Protocol v1
actions/events and use `host.integrations.register_action(name, handler)` and
`host.integrations.emit(user, event_name, payload)`. The Host namespaces public
actions as `<plugin-slug>.<name>`, resolves the AniMemo user from an authenticated
external identity binding, and enforces the normal USER installation boundary.
Integration handlers never receive connection secrets or HMAC material.

## Official plugin version immutability

An official plugin release is identified by `slug + version`. Once that identity
has entered a release, its publishable package payload is permanently frozen.
Any package-affecting change, including a manifest, frontend bundle, backend
runtime, or packaged asset change, must use a strictly newer SemVer version.

For example, changing the payload of `watch-history-importer` after publishing
`0.3.0` requires a bump such as `0.3.1`. CI and the production sync path compare
the canonical content digest of the actual packaged files: `manifest.json`, the
frontend bundle, backend runtime, and packaged assets. The descriptor contains
each POSIX path, byte size, and file SHA-256 in stable order. Packaging metadata
such as `package-index.json` is validated but is not itself part of this logical
content identity.

The canonical content digest and package blob SHA-256 are separate identities.
The content digest decides whether an immutable `slug + version` changed. The
blob SHA identifies the exact `.ajplugin` archive stored in CAS. Two valid ZIPs
may therefore have the same content digest but different blob SHA values because
of compression or ZIP metadata differences. That is not a version rewrite. An
already-published version continues to reference its original archived blob;
the sync command does not store or substitute the newly built equivalent ZIP.

Reusing `0.3.0` with a different canonical content digest is rejected. Restore
the original payload or publish a new version; never rewrite the old version.

This rule protects supply-chain identity, deterministic rollback, and database
references to published packages. It is not a cache workaround. Files excluded
from the real official package builder, such as plugin README documentation, do
not require a version bump by themselves.

The `plugins` CI job compares Base and Current with the same official package
file selection and canonical descriptor used by `build_official_package()` and
`sync_official_plugins`. Archive SHA values remain visible as diagnostics and as
the CAS binary identity, but they are not the immutable-version decision key.
