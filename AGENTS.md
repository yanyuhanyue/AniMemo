# AniMemo development

- This directory is the project root. Keep source, documentation and tooling in the directories below. All generated binaries, caches, databases, downloads, screenshots, logs and temporary files belong in `.local/` under this root. Never generate task files on the desktop, in Downloads, or in sibling project directories.
- Product implementation uses Go, TypeScript and PostgreSQL. Do not add another production language or a framework without a concrete requirement.
- `server/internal/journal` owns journal transactions and invariants. It must not import the HTTP or account modules. `internal/api` authenticates the caller and passes the authenticated owner to business operations.
- Runtime code must not import release, CI or developer tooling. Keep process and container orchestration in `tooling/` and `deploy/`.
- Edit `contracts/openapi.json`, run `npm run contracts`, and review the generated `web/src/api/schema.d.ts` when changing the API. Do not edit generated types by hand.
- Run checks that can catch the change: `npm run check:web`, `npm run check:api`, `npm test`, or `npm run test:api`. Database behavior and concurrency must be tested on PostgreSQL. Integration tests use their own random schemas and clean them up.
- Keep browser checks on the real API. Use synthetic accounts and data. Store all screenshots and browser state in `.local/`.
- Linux with a working Docker engine is the preferred environment. Check `docker info`; a Docker CLI alone does not prove the engine is available. Report unrun container checks accurately.
- Preserve other work. Delete only files whose ownership and irrelevance have been established. Do not introduce global tools, system services or remote publishing as part of ordinary implementation.
