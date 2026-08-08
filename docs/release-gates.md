# Release Gates

AniMemo uses complementary CI gates. They prevent unsafe releases earlier, but
they do not replace production runtime validation.

## Core CI

- `frontend`: builds the application and runs frontend tests.
- `backend`: compiles Python, runs Django checks, verifies no missing migrations,
  and runs the backend test suite.
- `postgres`: runs concurrency coverage against PostgreSQL and Redis.
- `plugins`: validates/builds plugin artifacts and enforces official package
  immutability.

The official plugin gate compares the canonical package content identity from
Base with Current. It hashes a versioned, stable descriptor of the files that
actually enter the official package. A canonical content change with the same
version fails; a strict SemVer increase passes; a downgrade or unplanned official
plugin removal fails. The logs print both the canonical content digest and the
exact archive SHA for diagnostics, together with the resolved Base SHA, Head SHA,
and resolution source.

Content identity is not archive identity. For example, the same payload may have
content digest `CCC` while a deflated archive has SHA `AAA` and a stored archive
has SHA `BBB`. The immutable version remains unchanged because its payload is the
same. CAS still addresses the actual archive bytes, and an already-published
official version retains the original `AAA` blob instead of being rewritten to
`BBB`.

## Fresh Docker release gate

The existing `docker` job validates `EMPTY DATABASE -> CURRENT RELEASE`. It
builds the production images, starts the full stack, checks `/health/`, and
checks the frontend root. This proves a fresh install works.

## Stateful upgrade gate

The `stateful-upgrade` job validates `BASE RELEASE -> CURRENT RELEASE` without
recreating persistent state:

1. Create an isolated Compose project and runner-temporary data root.
2. Check out Base in a detached Git worktree.
3. Build/start Base PostgreSQL, Redis, and API.
4. Seed a user, journal entry, user plugin installation, `watch_history`
   `PluginData`, official project/version/blob/deployment, CAS, and runtime.
5. Build Current and replace only the API container.
6. Let the normal container command run migrations, `sync_official_plugins`,
   static collection, and Gunicorn against the existing data.
7. Verify migrations, health, seeded state, immutable versions, original official
   PackageBlob retention, CAS, deployment, runtime reconciliation, and Integration
   Protocol migration coverage.
8. Restart Current API once and verify the state again.

The job never runs `down -v` between Base and Current. Cleanup is scoped to the
Compose project label, its temporary worktree, and its temporary data directory.
It never runs Docker system, volume, or network prune commands.

## Base resolution

Base/Head resolution is shared by both gates:

- Pull request: `github.event.pull_request.base.sha` -> `github.sha`.
- Push: `github.event.before` -> `github.sha`.
- New-branch/all-zero push: `HEAD^` fallback.
- `workflow_dispatch`: optional `upgrade_base_sha`, otherwise `HEAD^`.
- Local invocation: explicit refs are preferred; otherwise `HEAD^`.

`HEAD^` is only a convenience fallback. It is not proof of the currently
deployed production release. Before a release, manually dispatch Release Gate
with `upgrade_base_sha` set to the exact last deployed production commit when
that commit differs from the previous commit on `main`.

## Local commands

Resolve and audit the comparison refs:

```bash
python scripts/ci_refs.py --base <known-base-sha> --head HEAD
```

Run the artifact-only gate after building the official frontend artifact:

```bash
python scripts/pluginctl.py build watch-history-importer
python scripts/check_official_plugin_immutability.py --base <known-base-sha> --head HEAD --head-root .
```

Run the Docker upgrade gate when Bash and Docker Compose are available:

```bash
bash scripts/stateful-upgrade-gate.sh --base <known-base-sha> --head HEAD --current-root "$PWD"
```

The stateful script uses CI-only secrets and runner-temporary bind mounts. It is
not a production deployment script and must not be pointed at production data.
