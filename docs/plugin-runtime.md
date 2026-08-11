# Plugin Runtime

The browser requests `/api/v1/plugins/enabled/`, dynamically imports each enabled
`frontendEntry`, injects its optional stylesheet, and isolates failures per
plugin. Disabling or unloading a plugin calls `dispose()` and removes its
navigation and routes without reloading the core application.

Plugin Platform v3 uses a clean initial migration. A local development database
that still contains the obsolete v2 `plugin_host_*` schema must be reset
manually before running `python manage.py migrate`. Production startup never
drops plugin tables or rewrites Django migration history automatically.
