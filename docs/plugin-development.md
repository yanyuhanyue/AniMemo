# Plugin Development

Run `python scripts/pluginctl.py validate`, then `build <slug>` and `pack <slug>`.
Do not add dependency installation to a plugin package. Keep API calls behind
the host facade, namespace plugin events as `plugin:<slug>:*`, and declare all
external network, upload and personal-data behavior in `dataPolicy`.

Backend Runtime Plugins are trusted in-process Python code installed by a superuser. `dataPolicy` is declarative auditing metadata, not an operating-system sandbox, and it does not technically prevent plugin code from bypassing Host helpers. Prefer `host.request_json()`, `host.storage`, `host.api`, and `host.register_hook()` so policy and lifecycle behavior remain reviewable.
