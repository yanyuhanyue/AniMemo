# AniMemo Instance-Scoped Deployment Contract v2

Status: FROZEN FOR v1.1 INSTANCE-SCOPED RELEASES

`schemaVersion = 2`; deployment profile is exactly
`v1.1-instance-scoped`. Release Authority remains
`GITHUB_IMMUTABLE_RELEASE`; Portable archives remain `TRANSPORT_ONLY`.
Backup Format remains independently versioned at `schemaVersion = 1`.

## Instance name policy

`InstanceName` is 1–32 lowercase ASCII characters matching
`^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$`. `default` and `v1-1-rc` are valid.
The service and Compose role names `api`, `web`, `postgres`, `redis`, `updater`,
`root`, `system`, `instances`, `current`, `previous`, `releases`, `bootstrap`,
`cache`, and `runtime` are reserved. Unicode, case variants, escapes, traversal,
shell syntax, systemd syntax, and Docker-invalid names fail before discovery.

## Canonical templates

For `<name>`:

- app: `/opt/animemo-instances/<name>`
- data: `/data/animemo-instances/<name>`
- updater state: `/var/lib/animemo-updater/instances/<name>`
- updater runtime: `/run/animemo-updater/<name>`
- config: `<data>/config/animemo.json`
- backup: `<data>/backups`
- locator: `<state>/instance.json`
- socket: `<runtime>/updater.sock`
- Compose project: `animemo-<name>`
- updater unit: `animemo-updater@<name>.service`

The shared updater program root is `/opt/animemo-updater`. No caller may
override a root. cwd, environment, legacy roots, Docker enumeration, image
names, and symlinks are not discovery sources. The old singleton profile is
`PRE_V1_1_SINGLETON_PROFILE = UNSUPPORTED_NOT_AUTO_ADOPTED`.

## Locator and ownership

Locator schema v2 is closed and binds `instanceName`, UUID `instanceId`, every
canonical path, Compose project, updater unit/socket, listen identity, Public
Origin, managed configuration revision, exact release identity, and the digest
of one closed ownership receipt. Unknown or duplicate fields, schema v1,
noncanonical paths, secrets, symlinks, junctions, multiple hard links, unsafe
ownership/mode, concurrent replacement, or receipt mismatch fail closed.

The ownership receipt binds the same instance identity and namespace plus
owned containers, networks, volumes and files, exact release identity, listen
identity, creation time, and its canonical digest. Mutation, rollback, and any
future removal require locator + receipt + matching live labels/digests. Names
alone never authorize deletion or adoption.

## Runtime isolation

Compose is always called with `--project-name animemo-<name>`. Every container
and network has exact `io.animemo.instance-name`, `io.animemo.instance-id`, and
`io.animemo.compose-project` labels. Bind mounts use only the instance data and
runtime roots. The writer-service allowlist is `postgres`, `redis`, and `api`;
`web` receives media read-only. No cross-instance state, socket, config,
operation journal, lock, release slot, runtime environment, cache, or backup is
readable through an AniMemo binding.

The updater uses the `animemo-updater@.service` template. `%i` is accepted only
after the closed `InstanceName` validator. `StateDirectory`, `RuntimeDirectory`,
`ReadOnlyPaths`, and `ReadWritePaths` expand only the canonical templates; no
arbitrary `EnvironmentFile` or shell command is used. Managed config and
`managed.env` are protected atomic single-link files bound to the instance,
config revision, and locator digest.

## Side-by-side and restore

Different valid names produce disjoint roots, projects, sockets, units, locks,
operations, slots, and configuration. Install and restore-to-new reject any
foreign, partial, symlinked, linked, casefold-colliding, already owned, running
project/unit, or occupied-listen target before mutation. A restored backup may
name a different source instance, but the target identity and namespace are
new and empty.
