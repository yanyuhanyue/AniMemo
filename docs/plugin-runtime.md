# Plugin Runtime

The browser requests `/api/plugins/enabled/`, dynamically imports each enabled
`frontendEntry`, injects its optional stylesheet, and isolates failures per
plugin. Disabling or unloading a plugin calls `dispose()` and removes its
navigation and routes without reloading the core application.
