# `.ajplugin` Package v1

An `.ajplugin` is a ZIP container with a root `manifest.json`, optional
`frontend/plugin.js` and `frontend/plugin.css`, and a `backend/` runtime tree.
The package index records SHA-256 for every file. Installers reject traversal,
absolute paths, duplicate members, symlinks, encrypted entries, oversized
archives and malformed manifests before extraction.

Official immutable-version checks use a host-side canonical content digest, not
the SHA-256 of the ZIP container. The canonical descriptor covers the actual
payload files declared by the validated index, including `manifest.json`, but
excludes `package-index.json` itself. It stores each POSIX path, byte size, and
file SHA-256 in stable order under `contentIdentityVersion: 1` before hashing
canonical JSON. ZIP compression, timestamps, and archive metadata therefore do
not affect logical content identity.

The package blob SHA-256 remains unchanged in purpose: it addresses the exact
`.ajplugin` bytes in CAS and verifies downloads and storage. Equal canonical
content does not merge CAS blobs generally. During official sync, an equivalent
new archive is simply not stored for an existing `slug + version`; the historical
blob remains authoritative.

Package storage and garbage collection serialize on the same blob SHA lock.
Storage keeps that lock until the blob registration commits; version uploads
and official registration commit the protecting version in the same boundary.
GC rechecks the row and its references under that lock before removing bytes.
A rolled-back GC deletion restores the canonical file from its tombstone.

The host's standalone blob-store and GC services must own their transaction
commit. They reject an enclosing application transaction or manually disabled
autocommit before changing package bytes. Business registration uses the shared
internal transaction boundary; callers must not register a version after a
standalone store inside a separate transaction. Publication follows the completed
official registration transaction.
