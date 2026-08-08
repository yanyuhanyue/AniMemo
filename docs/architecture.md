# Anime Journal Pre-1.0 Architecture

The application is split into five runtime boundaries:

- `accounts`: the canonical `accounts.User` model and authentication identity.
- `journal`: user journals, columns, catalog records and site-facing workflows.
- `site`: HTTP/API composition and public presentation.
- `plugin_host`: Manifest v2 validation, `.ajplugin` storage, lifecycle state and data isolation.
- `config`: settings, security middleware and process-start backend runtime discovery.

Plugins are loaded only after validation. Backend code is allowlisted at process start; frontend code is loaded from the enabled-plugin API using the declared `frontend/plugin.js` entry. No runtime dependency installation is performed.
