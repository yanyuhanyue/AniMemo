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
`0.3.0` requires a bump such as `0.3.1`. Reusing `0.3.0` with a different package
SHA-256 is rejected by CI and by the production sync path. Restore the original
payload or publish a new version; never rewrite the old version.

This rule protects supply-chain identity, deterministic rollback, and database
references to published packages. It is not a cache workaround. Files excluded
from the real official package builder, such as plugin README documentation, do
not require a version bump by themselves.

The `plugins` CI job builds the current official artifact and compares it with
the resolved base release by using the same `build_official_package()` function
as `sync_official_plugins`.
