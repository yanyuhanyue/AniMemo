# Plugin Development

Run `python scripts/pluginctl.py validate`, then `build <slug>` and `pack <slug>`.
Do not add dependency installation to a plugin package. Keep API calls behind
the host facade, namespace plugin events as `plugin:<slug>:*`, and declare all
external network, upload and personal-data behavior in `dataPolicy`.

Backend Runtime Plugins are trusted in-process Python code installed by a superuser. `dataPolicy` is declarative auditing metadata, not an operating-system sandbox, and it does not technically prevent plugin code from bypassing Host helpers. Prefer `host.request_json()`, `host.storage`, `host.api`, and `host.register_hook()` so policy and lifecycle behavior remain reviewable.

USER plugins may declare only `journal.after_create`, `journal.after_update`,
`journal.after_delete`, `column.after_publish`, and `column.after_delete`.
The Host resolves the event owner and runs the callback only when that user has
an enabled `UserPluginInstallation`. Registration and `user.*` lifecycle hooks
are SYSTEM-only.
