# `.ajplugin` Package v1

An `.ajplugin` is a ZIP container with a root `manifest.json`, optional
`frontend/plugin.js` and `frontend/plugin.css`, and a `backend/` runtime tree.
The package index records SHA-256 for every file. Installers reject traversal,
absolute paths, duplicate members, symlinks, encrypted entries, oversized
archives and malformed manifests before extraction.
