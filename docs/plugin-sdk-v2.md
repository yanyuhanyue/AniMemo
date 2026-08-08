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
