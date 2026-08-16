from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from durability.platform import (
    PLATFORM_QUALIFICATION_SCHEMA,
    REQUIRED_CAPABILITIES,
    REQUIRED_REHEARSALS,
    STANDARD_PLATFORM_PROFILE,
    PlatformQualification,
    PlatformQualificationError,
    canonical_platform_qualification_bytes,
    finalize_platform_qualification,
    read_platform_qualification,
)
from release.contract import (
    POSTGRES_DIGEST,
    POSTGRES_REPOSITORY,
    REDIS_DIGEST,
    REDIS_REPOSITORY,
)

QUALIFIED_POSTGRES_IMAGE = f"{POSTGRES_REPOSITORY}@{POSTGRES_DIGEST}"
QUALIFIED_REDIS_IMAGE = f"{REDIS_REPOSITORY}@{REDIS_DIGEST}"
QUALIFICATION_WORKFLOW_PATH = ".github/workflows/release.yml"
_WORKFLOW_REF = re.compile(
    r"^[^/]+/[^/]+/(?P<path>\.github/workflows/[^@]+)@(?P<ref>.+)$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


class QualificationProbeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


Command = Callable[[Sequence[str]], str]
_QUALIFICATION_EXECUTABLES = {
    "docker": "docker",
    "pg_dump": "pg_dump",
    "psql": "psql",
    "sudo": "sudo",
    "systemd": "systemd",
}


def _fail(code: str) -> None:
    raise QualificationProbeError(code)


def _validated_probe_command(command: Sequence[str]) -> list[str]:
    if isinstance(command, (str, bytes)) or not command:
        _fail("PLATFORM_PROBE_COMMAND_REJECTED")
    values = list(command)
    if any(
        not isinstance(value, str)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in values
    ):
        _fail("PLATFORM_PROBE_COMMAND_REJECTED")
    executable = _QUALIFICATION_EXECUTABLES.get(values[0])
    if executable is None:
        _fail("PLATFORM_PROBE_COMMAND_REJECTED")
    return [executable, *values[1:]]


def _run(command: Sequence[str]) -> str:
    validated_command = _validated_probe_command(command)
    try:
        completed = subprocess.run(
            validated_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("PLATFORM_PROBE_COMMAND_FAILED")
    if completed.returncode != 0:
        _fail("PLATFORM_PROBE_COMMAND_FAILED")
    return completed.stdout.strip()


def _strict_json(path: Path) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


def _write_evidence(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_os_release(path: Path = Path("/etc/os-release")) -> tuple[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"ID", "VERSION_ID"}:
                values[key] = value.strip().strip('"')
    except OSError:
        _fail("PLATFORM_HOST_OBSERVATION_FAILED")
    if not values.get("ID") or not values.get("VERSION_ID"):
        _fail("PLATFORM_HOST_OBSERVATION_FAILED")
    return values["ID"], values["VERSION_ID"]


def _first_line(value: str, code: str) -> str:
    line = value.splitlines()[0].strip() if value else ""
    if not line or any(character in line for character in "\x00\r\n"):
        _fail(code)
    return line


def _major(value: str, code: str) -> int:
    match = re.search(r"PostgreSQL\)?\s+([0-9]+)(?:\.|\s|$)", value)
    if match is None:
        _fail(code)
    return int(match.group(1))


def _github_identity(
    candidate_sha: str, environ: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, object]]:
    if not _SHA.fullmatch(candidate_sha):
        _fail("PLATFORM_IDENTITY_INVALID")
    if (
        environ.get("GITHUB_ACTIONS") != "true"
        or environ.get("RUNNER_OS") != "Linux"
        or environ.get("RUNNER_ARCH") != "X64"
        or environ.get("GITHUB_SHA") != candidate_sha
        or environ.get("GITHUB_WORKFLOW_SHA") != candidate_sha
    ):
        _fail("PLATFORM_GITHUB_HOSTED_CONTEXT_REQUIRED")
    match = _WORKFLOW_REF.fullmatch(environ.get("GITHUB_WORKFLOW_REF", ""))
    if match is None or match.group("path") != QUALIFICATION_WORKFLOW_PATH:
        _fail("PLATFORM_WORKFLOW_IDENTITY_INVALID")
    run_id = environ.get("GITHUB_RUN_ID", "")
    attempt = environ.get("GITHUB_RUN_ATTEMPT", "")
    if (
        not run_id.isdigit()
        or run_id.startswith("0")
        or not attempt.isdigit()
        or attempt.startswith("0")
    ):
        _fail("PLATFORM_RUN_IDENTITY_INVALID")
    workflow = {
        "path": match.group("path"),
        "ref": match.group("ref"),
        "sha": candidate_sha,
    }
    return workflow, {"id": run_id, "attempt": int(attempt)}


def _rehearsal_evidence(directory: Path, candidate_sha: str) -> dict[str, str]:
    try:
        metadata = directory.lstat()
        names = {item.name for item in directory.iterdir()}
    except OSError:
        _fail("PLATFORM_REHEARSAL_EVIDENCE_INVALID")
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or names != set(REQUIRED_REHEARSALS)
    ):
        _fail("PLATFORM_REHEARSAL_EVIDENCE_INVALID")
    for name in REQUIRED_REHEARSALS:
        marker = directory / name
        try:
            marker_stat = marker.lstat()
            value = marker.read_text(encoding="ascii")
        except (OSError, UnicodeError):
            _fail("PLATFORM_REHEARSAL_EVIDENCE_INVALID")
        if (
            marker.is_symlink()
            or not stat.S_ISREG(marker_stat.st_mode)
            or marker_stat.st_nlink != 1
            or value != candidate_sha + "\n"
        ):
            _fail("PLATFORM_REHEARSAL_EVIDENCE_INVALID")
    return {name: "PASS" for name in REQUIRED_REHEARSALS}


def _filesystem_capabilities(root: Path) -> dict[str, bool]:
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        _fail("PLATFORM_FILESYSTEM_PROBE_FAILED")
    source = root / "source"
    replacement = root / "replacement"
    symbolic = root / "symbolic"
    descriptor = os.open(
        source,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, b"qualification")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    source_stat = source.lstat()
    if (
        not stat.S_ISREG(source_stat.st_mode)
        or source_stat.st_nlink != 1
        or stat.S_IMODE(source_stat.st_mode) != 0o600
        or source_stat.st_uid != os.getuid()
        or source_stat.st_gid != os.getgid()
    ):
        _fail("PLATFORM_FILESYSTEM_PROBE_FAILED")
    symbolic.symlink_to(source)
    try:
        linked = os.open(symbolic, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        if error.errno not in {errno.ELOOP, errno.EMLINK}:
            _fail("PLATFORM_FILESYSTEM_PROBE_FAILED")
    else:
        os.close(linked)
        _fail("PLATFORM_FILESYSTEM_PROBE_FAILED")
    replacement.write_bytes(b"replacement")
    with replacement.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(replacement, source)
    directory_descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    if source.read_bytes() != b"replacement":
        _fail("PLATFORM_FILESYSTEM_PROBE_FAILED")
    socket_path = root / "qualification.sock"
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        unix_socket.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        socket_stat = socket_path.lstat()
        if (
            not stat.S_ISSOCK(socket_stat.st_mode)
            or stat.S_IMODE(socket_stat.st_mode) != 0o660
        ):
            _fail("PLATFORM_FILESYSTEM_PROBE_FAILED")
    finally:
        unix_socket.close()
    loopback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        loopback.bind(("127.0.0.1", 0))
        if loopback.getsockname()[0] != "127.0.0.1":
            _fail("PLATFORM_FILESYSTEM_PROBE_FAILED")
    finally:
        loopback.close()
    return {
        "directory_fsync": True,
        "file_fsync": True,
        "loopback_port_binding": True,
        "nofollow_regular_file": True,
        "posix_owner_mode": True,
        "same_directory_atomic_replace": True,
        "single_link_file": True,
        "unix_socket_permissions": True,
    }


def _assert_exact_image(command: Command, image: str) -> None:
    command(("docker", "pull", "--quiet", image))
    raw = command(
        ("docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}")
    )
    try:
        repo_digests = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("PLATFORM_IMAGE_IDENTITY_FAILED")
    expected_digest = image.rsplit("@", 1)[1]
    if not isinstance(repo_digests, list) or not any(
        isinstance(item, str) and item.endswith("@" + expected_digest)
        for item in repo_digests
    ):
        _fail("PLATFORM_IMAGE_IDENTITY_FAILED")


def _docker_capabilities(
    command: Command,
    *,
    postgres_image: str,
    redis_image: str,
    run_identity: str,
) -> tuple[dict[str, bool], int]:
    if (
        postgres_image != QUALIFIED_POSTGRES_IMAGE
        or redis_image != QUALIFIED_REDIS_IMAGE
    ):
        _fail("PLATFORM_IMAGE_AUTHORITY_MISMATCH")
    command(("docker", "info", "--format", "{{.ServerVersion}}"))
    _assert_exact_image(command, postgres_image)
    _assert_exact_image(command, redis_image)
    postgres_major = _major(
        command(("docker", "run", "--rm", postgres_image, "postgres", "--version")),
        "PLATFORM_POSTGRES_VERSION_INVALID",
    )
    if postgres_major != 16:
        _fail("PLATFORM_POSTGRES_VERSION_INVALID")
    project = "animemo-platform-" + re.sub(r"[^a-z0-9_-]", "-", run_identity.lower())
    with tempfile.TemporaryDirectory(prefix="animemo-platform-compose-") as directory:
        compose_file = Path(directory) / "compose.yml"
        compose_file.write_text(
            "services:\n"
            "  redis:\n"
            f"    image: {redis_image}\n"
            "    profiles: [qualification]\n"
            "    command: [redis-server, --save, '', --appendonly, 'no']\n"
            "    healthcheck:\n"
            "      test: [CMD, redis-cli, ping]\n"
            "      interval: 1s\n"
            "      timeout: 1s\n"
            "      retries: 30\n",
            encoding="utf-8",
        )
        base = ("docker", "compose", "-f", str(compose_file), "-p", project)
        try:
            profiles = command((*base, "config", "--profiles"))
            if "qualification" not in profiles.splitlines():
                _fail("PLATFORM_COMPOSE_PROBE_FAILED")
            command(
                (
                    *base,
                    "--profile",
                    "qualification",
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "60",
                )
            )
            services = command((*base, "ps", "--status", "running", "--services"))
            if services.splitlines() != ["redis"]:
                _fail("PLATFORM_COMPOSE_PROBE_FAILED")
        finally:
            try:
                command(
                    (
                        *base,
                        "--profile",
                        "qualification",
                        "down",
                        "-v",
                        "--remove-orphans",
                    )
                )
            except QualificationProbeError:
                pass
    return {
        "compose_profiles": True,
        "compose_v2": True,
        "compose_wait": True,
        "docker_daemon": True,
        "immutable_image_digest": True,
    }, postgres_major


def _systemd_capability(command: Command, run_identity: str) -> bool:
    unit = "animemo-platform-" + re.sub(r"[^a-z0-9-]", "-", run_identity.lower())
    command(
        (
            "sudo",
            "-n",
            "systemd-run",
            f"--unit={unit}",
            "--property=Type=oneshot",
            "--wait",
            "--collect",
            "/usr/bin/true",
        )
    )
    return True


def _postgres_capabilities(
    command: Command,
    *,
    source_url: str,
    target_url: str,
    run_identity: str,
) -> tuple[dict[str, bool], dict[str, object]]:
    pg_dump_major = _major(
        command(("pg_dump", "--version")), "PLATFORM_POSTGRES_TOOLS_INVALID"
    )
    psql_major = _major(
        command(("psql", "--version")), "PLATFORM_POSTGRES_TOOLS_INVALID"
    )
    if pg_dump_major != 16 or psql_major != 16:
        _fail("PLATFORM_POSTGRES_TOOLS_INVALID")
    source_major = (
        int(
            command(
                (
                    "psql",
                    "--dbname",
                    source_url,
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SHOW server_version_num",
                )
            )
        )
        // 10000
    )
    target_major = (
        int(
            command(
                (
                    "psql",
                    "--dbname",
                    target_url,
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SHOW server_version_num",
                )
            )
        )
        // 10000
    )
    if source_major != 16 or target_major != 16:
        _fail("PLATFORM_POSTGRES_VERSION_INVALID")
    schema = "animemo_platform_" + re.sub(r"[^a-z0-9_]", "_", run_identity.lower())
    with tempfile.TemporaryDirectory(prefix="animemo-platform-pg-") as directory:
        dump = Path(directory) / "qualification.sql"
        try:
            command(
                (
                    "psql",
                    "--dbname",
                    source_url,
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--command",
                    f"CREATE SCHEMA {schema}; CREATE TABLE {schema}.probe (value integer NOT NULL); INSERT INTO {schema}.probe VALUES (1);",
                )
            )
            command(
                (
                    "pg_dump",
                    "--dbname",
                    source_url,
                    "--format=plain",
                    "--no-owner",
                    "--no-privileges",
                    f"--schema={schema}",
                    f"--file={dump}",
                )
            )
            if (
                not dump.is_file()
                or dump.stat().st_size <= 0
                or tarfile.is_tarfile(dump)
            ):
                _fail("PLATFORM_POSTGRES_PLAIN_DUMP_FAILED")
            command(
                (
                    "psql",
                    "--dbname",
                    target_url,
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--file",
                    str(dump),
                )
            )
            count = command(
                (
                    "psql",
                    "--dbname",
                    target_url,
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    f"SELECT count(*) FROM {schema}.probe",
                )
            )
            if count != "1":
                _fail("PLATFORM_POSTGRES_RESTORE_FAILED")
        finally:
            for database_url in (source_url, target_url):
                try:
                    command(
                        (
                            "psql",
                            "--dbname",
                            database_url,
                            "--command",
                            f"DROP SCHEMA IF EXISTS {schema} CASCADE",
                        )
                    )
                except QualificationProbeError:
                    pass
    return {
        "postgres_plain_dump": True,
        "postgres_psql_restore": True,
    }, {
        "dumpFormat": "plain",
        "sourceServerMajor": source_major,
        "pgDumpMajor": pg_dump_major,
        "psqlMajor": psql_major,
        "targetServerMajor": target_major,
    }


def collect_platform_qualification(
    *,
    candidate_sha: str,
    postgres_image: str,
    redis_image: str,
    source_database_url: str,
    target_database_url: str,
    rehearsal_directory: Path,
    probe_root: Path,
    environ: Mapping[str, str] = os.environ,
    command: Command = _run,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PlatformQualification:
    workflow, run = _github_identity(candidate_sha, environ)
    run_identity = f"{run['id']}-{run['attempt']}"
    rehearsals = _rehearsal_evidence(rehearsal_directory, candidate_sha)
    distribution_id, distribution_version = _read_os_release()
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        _fail("PLATFORM_HOST_UNSUPPORTED")
    host = {
        "os": "linux",
        "architecture": "amd64",
        "distributionId": distribution_id,
        "distributionVersion": distribution_version,
        "kernel": platform.release(),
        "systemdVersion": _first_line(
            command(("systemd", "--version")), "PLATFORM_SYSTEMD_PROBE_FAILED"
        ),
        "dockerVersion": _first_line(
            command(("docker", "version", "--format", "{{.Server.Version}}")),
            "PLATFORM_DOCKER_PROBE_FAILED",
        ),
        "composeVersion": _first_line(
            command(("docker", "compose", "version", "--short")),
            "PLATFORM_COMPOSE_PROBE_FAILED",
        ),
    }
    capabilities = _filesystem_capabilities(probe_root)
    docker_capabilities, image_postgres_major = _docker_capabilities(
        command,
        postgres_image=postgres_image,
        redis_image=redis_image,
        run_identity=run_identity,
    )
    capabilities.update(docker_capabilities)
    capabilities["systemd_unit_lifecycle"] = _systemd_capability(command, run_identity)
    postgres_capabilities, database_path = _postgres_capabilities(
        command,
        source_url=source_database_url,
        target_url=target_database_url,
        run_identity=run_identity,
    )
    capabilities.update(postgres_capabilities)
    if image_postgres_major != database_path["sourceServerMajor"]:
        _fail("PLATFORM_POSTGRES_VERSION_INVALID")
    if tuple(sorted(capabilities)) != REQUIRED_CAPABILITIES or not all(
        capabilities.values()
    ):
        _fail("PLATFORM_CAPABILITY_NOT_QUALIFIED")
    observed_at = clock().astimezone(UTC).replace(microsecond=0)
    payload = {
        "schema": PLATFORM_QUALIFICATION_SCHEMA,
        "profile": STANDARD_PLATFORM_PROFILE,
        "candidateSha": candidate_sha,
        "workflow": workflow,
        "run": run,
        "observedAt": observed_at.isoformat().replace("+00:00", "Z"),
        "host": host,
        "databasePath": database_path,
        "imageDigests": {"postgres": postgres_image, "redis": redis_image},
        "capabilities": capabilities,
        "rehearsals": rehearsals,
    }
    return finalize_platform_qualification(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="platform_qualification.py")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--candidate-sha", required=True)
    collect.add_argument("--postgres-image", required=True)
    collect.add_argument("--redis-image", required=True)
    collect.add_argument("--source-database-url", required=True)
    collect.add_argument("--target-database-url", required=True)
    collect.add_argument("--rehearsal-directory", type=Path, required=True)
    collect.add_argument("--probe-root", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--input", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--candidate-sha", required=True)
    verify.add_argument("--run-id")
    verify.add_argument("--run-attempt", type=int)
    verify.add_argument("--workflow-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "collect":
            qualification = collect_platform_qualification(
                candidate_sha=args.candidate_sha,
                postgres_image=args.postgres_image,
                redis_image=args.redis_image,
                source_database_url=args.source_database_url,
                target_database_url=args.target_database_url,
                rehearsal_directory=args.rehearsal_directory,
                probe_root=args.probe_root,
            )
            _write_evidence(
                args.output, canonical_platform_qualification_bytes(qualification)
            )
        elif args.command == "finalize":
            payload = _strict_json(args.input)
            if not isinstance(payload, dict):
                raise PlatformQualificationError("PLATFORM_SCHEMA_INVALID")
            qualification = finalize_platform_qualification(payload)
            _write_evidence(
                args.output, canonical_platform_qualification_bytes(qualification)
            )
        else:
            qualification = read_platform_qualification(args.input)
            if qualification.candidate_sha != args.candidate_sha:
                raise PlatformQualificationError("PLATFORM_CANDIDATE_MISMATCH")
            if args.run_id is not None and qualification.run["id"] != args.run_id:
                raise PlatformQualificationError("PLATFORM_RUN_MISMATCH")
            if (
                args.run_attempt is not None
                and qualification.run["attempt"] != args.run_attempt
            ):
                raise PlatformQualificationError("PLATFORM_RUN_MISMATCH")
            if (
                args.workflow_path is not None
                and qualification.workflow["path"] != args.workflow_path
            ):
                raise PlatformQualificationError("PLATFORM_WORKFLOW_MISMATCH")
    except (
        OSError,
        ValueError,
        PlatformQualificationError,
        QualificationProbeError,
    ) as error:
        code = (
            error.code
            if isinstance(error, (PlatformQualificationError, QualificationProbeError))
            else "PLATFORM_EVIDENCE_UNREADABLE"
        )
        print(json.dumps({"error": {"code": code}}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "schema": qualification.schema,
                "profile": qualification.profile,
                "candidateSha": qualification.candidate_sha,
                "evidenceDigest": qualification.evidence_digest,
                "status": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
