from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from release.producer_toolchain import (
    DOCKERFILE_PATH,
    LOCK_PATH,
    ProducerToolchainError,
    validate_producer_toolchain_receipt,
)


ROOT = LOCK_PATH.parents[1]
WRAPPER_PATH = ROOT / "scripts" / "run-in-release-producer.sh"
ENTRYPOINT_PATH = ROOT / "scripts" / "release-producer-entrypoint.sh"
RUNTIME_READINESS_PATH = (
    ROOT / "scripts" / "release-producer-runtime-readiness.sh"
)


def _completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=kwargs.pop("timeout", 120),
        **kwargs,
    )


def _working_bash() -> str | None:
    candidates: list[str] = []
    if os.name == "nt":
        candidates.append(r"C:\Program Files\Git\bin\bash.exe")
    discovered = shutil.which("bash")
    if discovered is not None:
        candidates.append(discovered)
    for candidate in dict.fromkeys(candidates):
        if not Path(candidate).is_file():
            continue
        probe = _completed(
            [
                candidate,
                "-lc",
                "set -e; d=$(mktemp -d); trap 'rm -rf -- \"$d\"' EXIT; "
                "chmod 0700 \"$d\"; test \"$(stat -c '%a' \"$d\")\" = 700; "
                "command -v mountpoint >/dev/null",
            ],
            encoding="utf-8",
            errors="replace",
        )
        if probe.returncode == 0:
            return candidate
    return None


class ProducerToolchainReceiptTests(unittest.TestCase):
    candidate_sha = "a" * 40

    def test_entrypoint_binds_embedded_inputs_to_the_mounted_workspace(self):
        entrypoint = (
            LOCK_PATH.parents[1] / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"$GITHUB_WORKSPACE/release/requirements.lock"', entrypoint)
        self.assertIn(
            '"$GITHUB_WORKSPACE/deploy/release-producer.Dockerfile"',
            entrypoint,
        )
        self.assertNotIn("/workspace/release/requirements.lock", entrypoint)

    def test_go_temp_execution_is_scoped_to_a_private_runner_directory(self):
        wrapper = (
            LOCK_PATH.parents[1] / "scripts" / "run-in-release-producer.sh"
        ).read_text(encoding="utf-8")
        entrypoint = (
            LOCK_PATH.parents[1] / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("animemo-release-producer-session.XXXXXXXXXX", wrapper)
        self.assertIn('producer_gotmp="$producer_session/go-tmp"', wrapper)
        self.assertIn(
            "--tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777",
            wrapper,
        )
        self.assertIn('--env "GOTMPDIR=$producer_gotmp"', wrapper)
        self.assertNotIn("|GOTMPDIR|", wrapper)
        self.assertIn('go-tmp', entrypoint)
        self.assertIn('ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT', entrypoint)

    @staticmethod
    def _sha256(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _receipt(self) -> dict[str, object]:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        byte_lock = lock["byteAuthority"]
        return {
            "schemaVersion": "animemo.release-producer-toolchain-receipt.v1",
            "candidateSha": self.candidate_sha,
            "runner": {
                "label": "ubuntu-24.04",
                "os": "Linux",
                "arch": "X64",
                "imageOS": "ubuntu24",
                "imageVersion": "20260820.1.0",
                "observationOnly": True,
            },
            "byteAuthority": {
                "releaseProducer": {
                    "imageId": "sha256:" + "b" * 64,
                    "dockerfileSha256": self._sha256(DOCKERFILE_PATH),
                },
                "python": byte_lock["python"]["hostedRuntimeVersion"],
                "go": "go" + byte_lock["go"]["version"],
                "buildx": byte_lock["buildx"]["version"],
                "buildkit": byte_lock["buildkit"]["version"],
                "buildkitImage": byte_lock["buildkit"]["image"],
                "backendImage": byte_lock["python"]["backendImage"],
                "nodeImage": byte_lock["node"]["image"],
                "npm": byte_lock["npm"],
            },
            "toolchainLockSha256": self._sha256(LOCK_PATH),
        }

    def _write(self, root: Path, value: object) -> Path:
        target = root / "receipt.json"
        target.write_text(json.dumps(value), encoding="utf-8")
        return target

    def test_exact_receipt_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self._write(Path(temporary), self._receipt())
            validated = validate_producer_toolchain_receipt(
                target,
                expected_candidate_sha=self.candidate_sha,
            )
            self.assertEqual(validated["candidateSha"], self.candidate_sha)

    def test_authority_and_runner_tamper_fail_closed(self) -> None:
        mutations = (
            ("runner observation", ("runner", "observationOnly"), False),
            ("runner identity", ("runner", "imageVersion"), ""),
            (
                "producer dockerfile",
                ("byteAuthority", "releaseProducer", "dockerfileSha256"),
                "sha256:" + "0" * 64,
            ),
            ("python", ("byteAuthority", "python"), "3.12.11"),
            ("lock", ("toolchainLockSha256",), "sha256:" + "0" * 64),
        )
        for label, path, replacement in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                value = copy.deepcopy(self._receipt())
                cursor = value
                for part in path[:-1]:
                    cursor = cursor[part]
                cursor[path[-1]] = replacement
                target = self._write(Path(temporary), value)
                with self.assertRaises(ProducerToolchainError):
                    validate_producer_toolchain_receipt(
                        target,
                        expected_candidate_sha=self.candidate_sha,
                    )


class ReleaseProducerImportAuthorityContractTests(unittest.TestCase):
    def test_wrapper_sets_one_fixed_repository_import_authority(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

        self.assertIn('--env "PYTHONSAFEPATH=1"', wrapper)
        self.assertEqual(wrapper.count('--env "PYTHONPATH=$GITHUB_WORKSPACE"'), 1)
        allowlist = wrapper[wrapper.index("allowed=") : wrapper.index("environment=()")]
        self.assertNotIn("PYTHONPATH", allowlist)
        self.assertNotIn("PYTHONSAFEPATH", allowlist)
        self.assertNotIn("${PYTHONPATH", wrapper)

    def test_entrypoint_requires_exact_workspace_module_provenance(self) -> None:
        entrypoint = ENTRYPOINT_PATH.read_text(encoding="utf-8")

        for authority in (
            "PYTHONSAFEPATH",
            "PYTHONPATH",
            "release.cli",
            "release.producer_toolchain",
            "scripts.formal_windows_pretrust",
            "scripts.release_authority",
            "find_spec",
            "sys.path",
            "resolve(strict=True)",
            "python -I -S -B",
            "sys.stdlib_module_names",
        ):
            self.assertIn(authority, entrypoint)
        self.assertIn("pwd -P", entrypoint)

    def test_entrypoint_allows_only_the_known_node_dependency_root(self) -> None:
        entrypoint = ENTRYPOINT_PATH.read_text(encoding="utf-8")
        lines = entrypoint.splitlines()
        start = lines.index(
            "python -I -S -B <<'ANIMEMO_IMPORT_AUTHORITY' || fail_import"
        )
        end = lines.index("ANIMEMO_IMPORT_AUTHORITY", start + 1)
        isolated_validator = "\n".join(lines[start + 1 : end])
        self.assertIn('candidate.name == "node_modules"', isolated_validator)
        self.assertIn("candidate.is_symlink()", isolated_validator)

        with tempfile.TemporaryDirectory(prefix="animemo-node-root-") as temporary:
            workspace = Path(temporary)
            (workspace / "release").mkdir()
            (workspace / "scripts").mkdir()
            (workspace / "node_modules").mkdir()
            for relative in (
                "release/__init__.py",
                "release/cli.py",
                "release/producer_toolchain.py",
                "scripts/formal_windows_pretrust.py",
                "scripts/release_authority.py",
            ):
                (workspace / relative).write_text("", encoding="utf-8")
            environment = {**os.environ, "GITHUB_WORKSPACE": str(workspace.resolve())}

            allowed = _completed(
                [sys.executable, "-I", "-S", "-B", "-c", isolated_validator],
                cwd=workspace,
                env=environment,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)

            (workspace / "jsonschema").mkdir()
            shadowed = _completed(
                [sys.executable, "-I", "-S", "-B", "-c", isolated_validator],
                cwd=workspace,
                env=environment,
            )
            self.assertEqual(shadowed.returncode, 2, shadowed.stderr)

            if os.name != "nt":
                (workspace / "jsonschema").rmdir()
                (workspace / "node_modules").rmdir()
                node_authority = workspace / ".node-authority"
                node_authority.mkdir()
                (workspace / "node_modules").symlink_to(
                    node_authority, target_is_directory=True
                )
                linked = _completed(
                    [sys.executable, "-I", "-S", "-B", "-c", isolated_validator],
                    cwd=workspace,
                    env=environment,
                )
                self.assertEqual(linked.returncode, 2, linked.stderr)


class ReleaseProducerRuntimeAuthorityContractTests(unittest.TestCase):
    def test_wrapper_declares_closed_go_runtime_contract(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

        for contract in (
            "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "GOTMPDIR",
            'GOENV=off',
            'GOTOOLCHAIN=local',
            'GOWORK=off',
            'GOPROXY=https://proxy.golang.org,direct',
            'GOSUMDB=sum.golang.org',
            'GOPRIVATE=',
            'GONOSUMDB=',
            'GONOPROXY=',
            'GOINSECURE=',
            'GOFLAGS=',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, wrapper)

    def test_wrapper_exports_exact_private_go_runtime_contract(self) -> None:
        bash = _working_bash()
        if bash is None:
            self.skipTest("a working bash runtime is unavailable")

        with tempfile.TemporaryDirectory(
            prefix="animemo-producer-wrapper-contract-"
        ) as temporary:
            temporary_root = Path(temporary)
            fake_bin = temporary_root / "fake-bin"
            fake_bin.mkdir()
            capture = temporary_root / "docker-run-argv.txt"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  printf '%s\\n' "$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID"
  exit 0
fi
if [[ "$1" == "run" ]]; then
  shift
  printf '%s\\n' "$@" > "$ANIMEMO_TEST_DOCKER_CAPTURE"
  exit 0
fi
exit 97
""",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)

            workspace = _completed(
                [bash, "-lc", "pwd -P"],
                cwd=ROOT,
                encoding="utf-8",
                errors="replace",
            )
            runner_temp = _completed(
                [bash, "-lc", "pwd -P"],
                cwd=temporary_root,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(workspace.returncode, 0, workspace.stderr)
            self.assertEqual(runner_temp.returncode, 0, runner_temp.stderr)

            image_id = "sha256:" + "b" * 64
            environment = {
                **os.environ,
                "PATH": os.pathsep.join((str(fake_bin), os.environ["PATH"])),
                "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": image_id,
                "ANIMEMO_TEST_DOCKER_CAPTURE": (
                    runner_temp.stdout.strip() + "/docker-run-argv.txt"
                ),
                "GITHUB_WORKSPACE": workspace.stdout.strip(),
                "RUNNER_TEMP": runner_temp.stdout.strip(),
                "GOPATH": "/ambient/go-path",
                "GOMODCACHE": "/ambient/go-module-cache",
                "GOCACHE": "/ambient/go-build-cache",
                "GOENV": "/ambient/go-env",
                "GOTOOLCHAIN": "auto",
                "GOWORK": "/ambient/go.work",
                "GOPROXY": "https://ambient.invalid",
                "GOPRIVATE": "ambient.invalid",
                "GONOSUMDB": "ambient.invalid",
                "GOINSECURE": "ambient.invalid",
            }
            result = _completed(
                [bash, "scripts/run-in-release-producer.sh", "--", "true"],
                cwd=ROOT,
                env=environment,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            arguments = capture.read_text(encoding="utf-8").splitlines()
            exported = {}
            for index, argument in enumerate(arguments[:-1]):
                if argument == "--env":
                    name, value = arguments[index + 1].split("=", 1)
                    exported[name] = value

            session_root = exported.get(
                "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT"
            )
            self.assertIsNotNone(
                session_root,
                "wrapper did not export a private Producer session root",
            )
            assert session_root is not None
            expected = {
                "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT": session_root,
                "HOME": f"{session_root}/home",
                "XDG_CACHE_HOME": f"{session_root}/xdg-cache",
                "XDG_CONFIG_HOME": f"{session_root}/xdg-config",
                "XDG_DATA_HOME": f"{session_root}/xdg-data",
                "XDG_STATE_HOME": f"{session_root}/xdg-state",
                "GH_CONFIG_DIR": f"{session_root}/xdg-config/gh",
                "GOPATH": f"{session_root}/go-path",
                "GOMODCACHE": f"{session_root}/go-module-cache",
                "GOCACHE": f"{session_root}/go-build-cache",
                "GOTMPDIR": f"{session_root}/go-tmp",
                "GOENV": "off",
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
                "GOPROXY": "https://proxy.golang.org,direct",
                "GOSUMDB": "sum.golang.org",
                "GOPRIVATE": "",
                "GONOSUMDB": "",
                "GONOPROXY": "",
                "GOINSECURE": "",
                "GOFLAGS": "",
            }
            self.assertEqual(
                {name: exported.get(name) for name in expected},
                expected,
            )
            self.assertIn("--read-only", arguments)

    def test_wrapper_cleanup_is_bound_to_the_original_session_identity(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")

        self.assertIn("producer_session_identity", wrapper)
        self.assertIn("stat -c '%d:%i'", wrapper)
        self.assertIn("mountpoint_status == 32", wrapper)
        self.assertIn("first_entry=", wrapper)
        self.assertIn("release producer Go session cleanup failed", wrapper)
        self.assertIn("command_status=70", wrapper)

    def test_wrapper_sessions_are_unique_and_replacement_cleanup_fails_closed(
        self,
    ) -> None:
        bash = _working_bash()
        if bash is None:
            self.skipTest("a POSIX-permission-capable bash runtime is unavailable")

        with tempfile.TemporaryDirectory(
            prefix="animemo-producer-session-behavior-"
        ) as temporary:
            runner_temp = Path(temporary).resolve()
            fake_bin = runner_temp / "fake-bin"
            fake_bin.mkdir()
            capture = runner_temp / "sessions.txt"
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  printf '%s\n' "$ANIMEMO_RELEASE_PRODUCER_IMAGE_ID"
  exit 0
fi
if [[ "$1" == "run" ]]; then
  shift
  session=""
  for argument in "$@"; do
    case "$argument" in
      ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT=*)
        session="${argument#*=}"
        ;;
    esac
  done
  test -n "$session"
  printf '%s\n' "$session" >> "$ANIMEMO_TEST_SESSION_CAPTURE"
  if [[ "${ANIMEMO_TEST_REPLACE_SESSION:-0}" == "1" ]]; then
    mv -- "$session" "$ANIMEMO_TEST_MOVED_SESSION"
    install -d -m 0700 -- "$session"
  fi
  exit "${ANIMEMO_TEST_BUSINESS_RC:-0}"
fi
exit 97
""",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            image_id = "sha256:" + "c" * 64
            environment = {
                **os.environ,
                "PATH": os.pathsep.join((str(fake_bin), os.environ["PATH"])),
                "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": image_id,
                "ANIMEMO_TEST_SESSION_CAPTURE": str(capture),
                "GITHUB_WORKSPACE": str(ROOT.resolve()),
                "RUNNER_TEMP": str(runner_temp),
            }

            for _ in range(2):
                result = _completed(
                    [bash, str(WRAPPER_PATH), "--", "true"],
                    cwd=ROOT,
                    env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            normal_sessions = capture.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(normal_sessions), 2)
            self.assertEqual(len(set(normal_sessions)), 2)
            for session in normal_sessions:
                self.assertFalse(Path(session).exists())
            self.assertTrue(
                (runner_temp / "animemo-release-producer-output").is_dir()
            )
            self.assertTrue(
                (runner_temp / "animemo-release-qualification-output").is_dir()
            )

            for label, business_rc, expected_rc in (
                ("successful-business-command", 0, 70),
                ("failed-business-command", 37, 37),
            ):
                with self.subTest(label=label):
                    moved_session = runner_temp / f"moved-{label}"
                    replaced = _completed(
                        [bash, str(WRAPPER_PATH), "--", "true"],
                        cwd=ROOT,
                        env={
                            **environment,
                            "ANIMEMO_TEST_REPLACE_SESSION": "1",
                            "ANIMEMO_TEST_MOVED_SESSION": str(moved_session),
                            "ANIMEMO_TEST_BUSINESS_RC": str(business_rc),
                        },
                    )
                    self.assertEqual(replaced.returncode, expected_rc)
                    self.assertEqual(
                        replaced.stderr.strip(),
                        "release producer Go session cleanup failed",
                    )
                    self.assertTrue(moved_session.is_dir())

    def test_entrypoint_closes_go_state_supply_and_module_authority(self) -> None:
        entrypoint = ENTRYPOINT_PATH.read_text(encoding="utf-8")

        for contract in (
            "release producer Go session authority is invalid",
            "release producer Go writable state is invalid",
            "release producer Go supply-chain environment is invalid",
            "release producer Go module authority is invalid",
            "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "GOMODCACHE",
            "GOCACHE",
            "GOTMPDIR",
            "/usr/local/go/bin/go",
            "go version go1.26.6 linux/amd64",
            "go telemetry off",
            "GOTELEMETRY",
            "GOTELEMETRYDIR",
            "animemo-release-producer-output",
            "animemo-release-qualification-output",
            "validate_output_staging",
            "stat -c '%d:%i'",
            "/proc/self/mountinfo",
            "root_mount_options",
            "require_not_mountpoint /go",
            "require_not_mountpoint /root",
            "release/release_attestation_verifier",
            "go.mod",
            "go.sum",
            "main.go",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, entrypoint)

        self.assertLess(
            entrypoint.index("go telemetry off"),
            entrypoint.index("python -I -S -B"),
        )

    def test_runtime_readiness_helper_has_only_closed_go_lifecycle_modes(self) -> None:
        helper = RUNTIME_READINESS_PATH.read_text(encoding="utf-8")

        for contract in (
            "check",
            "build-attestation-verifier",
            "release/release_attestation_verifier",
            "runtime-output/offline-release-verifier",
            "runtime-output/formal-release-verifier.exe",
            "release_attestation_verifier/offline-release-verifier",
            ".formal-pretrust-work/formal-release-verifier.exe",
            "go mod download",
            "GOPROXY=off",
            "GOSUMDB=off",
            "go mod verify",
            "go test -mod=readonly ./...",
            "CGO_ENABLED=0",
            "GOOS=linux",
            "GOOS=windows",
            "GOARCH=amd64",
            "go build -mod=readonly -trimpath",
            "go version -m",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, helper)

        self.assertNotIn("eval ", helper)
        self.assertNotIn("GOINSECURE=", helper)


@unittest.skipUnless(
    sys.platform.startswith("linux"),
    "the exact Producer image integration contract requires a Linux Docker host",
)
class RealReleaseProducerImageTests(unittest.TestCase):
    image_id = ""
    image_tag = ""
    runtime_root: tempfile.TemporaryDirectory[str] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        required_in_trusted_ci = os.environ.get("GITHUB_ACTIONS") == "true"
        docker = shutil.which("docker")
        availability_error = ""
        if docker is None:
            availability_error = "docker client is unavailable"
        else:
            info = _completed([docker, "info"], timeout=30)
            if info.returncode != 0:
                availability_error = "docker daemon is unavailable"
            else:
                buildx = _completed([docker, "buildx", "version"], timeout=30)
                if buildx.returncode != 0:
                    availability_error = "docker buildx is unavailable"
        if availability_error:
            if required_in_trusted_ci:
                raise AssertionError(
                    f"trusted Linux CI must run the real Producer image test: "
                    f"{availability_error}"
                )
            raise unittest.SkipTest(availability_error)

        cls.runtime_root = tempfile.TemporaryDirectory(
            prefix="animemo-producer-image-test-"
        )
        short_sha = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
        cls.image_tag = f"animemo-release-producer-import-test:{short_sha}"
        source_date_epoch = _completed(
            ["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT
        ).stdout.strip()
        build = _completed(
            [
                docker,
                "buildx",
                "build",
                "--load",
                "--provenance=false",
                "--platform",
                "linux/amd64",
                "--build-arg",
                f"SOURCE_DATE_EPOCH={source_date_epoch}",
                "--file",
                "deploy/release-producer.Dockerfile",
                "--tag",
                cls.image_tag,
                ".",
            ],
            cwd=ROOT,
            timeout=900,
        )
        if build.returncode != 0:
            raise AssertionError(
                "exact Producer Dockerfile build failed: "
                + build.stderr[-2000:]
            )
        inspect = _completed(
            [docker, "image", "inspect", "--format", "{{.Id}}", cls.image_tag],
            timeout=30,
        )
        cls.image_id = inspect.stdout.strip()
        if inspect.returncode != 0 or not cls.image_id.startswith("sha256:"):
            raise AssertionError("exact Producer image identity was not available")
        print(f"REAL_PRODUCER_IMAGE_ID={cls.image_id}")

    @classmethod
    def tearDownClass(cls) -> None:
        docker = shutil.which("docker")
        if docker and cls.image_tag:
            _completed([docker, "image", "rm", "--force", cls.image_tag], timeout=60)
        if cls.runtime_root is not None:
            cls.runtime_root.cleanup()
        super().tearDownClass()

    def _wrapper(
        self,
        *command: str,
        cwd: Path | None = None,
        ambient_pythonpath: str = "/tmp/ambient-shadow-root",
    ) -> subprocess.CompletedProcess[str]:
        assert self.runtime_root is not None
        environment = os.environ.copy()
        environment.update(
            {
                "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": self.image_id,
                "GITHUB_WORKSPACE": str(ROOT),
                "RUNNER_TEMP": self.runtime_root.name,
                "PYTHONPATH": ambient_pythonpath,
                "PYTHONSAFEPATH": "caller-value-must-not-cross-boundary",
                "GOPATH": "/tmp/ambient-go-path",
                "GOMODCACHE": "/tmp/ambient-go-module-cache",
                "GOCACHE": "/tmp/ambient-go-build-cache",
                "GOTMPDIR": "/tmp/ambient-go-tmp",
                "GOENV": "/tmp/ambient-go-env",
                "GOTOOLCHAIN": "auto",
                "GOWORK": "/tmp/ambient-go.work",
                "GOPROXY": "https://ambient.invalid",
                "GOSUMDB": "off",
                "GOPRIVATE": "ambient.invalid",
                "GONOSUMDB": "ambient.invalid",
                "GONOPROXY": "ambient.invalid",
                "GOINSECURE": "ambient.invalid",
                "GOFLAGS": "-mod=mod",
                "GOTELEMETRY": "on",
                "GOTELEMETRYDIR": "/tmp/ambient-telemetry",
            }
        )
        return _completed(
            ["bash", str(WRAPPER_PATH), "--", *command],
            cwd=cwd or ROOT,
            env=environment,
            timeout=900,
        )

    def _direct_entrypoint(
        self,
        *,
        pythonpath: str,
        pythonsafepath: str = "1",
        workspace: Path = ROOT,
        state_overrides: dict[str, str] | None = None,
        poison_relative: str | None = None,
        linked_relative: str | None = None,
        session_mode: int | None = None,
        symlink_session: bool = False,
        container_user: str | None = None,
        extra_mounts: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        assert self.runtime_root is not None
        docker = shutil.which("docker")
        assert docker is not None
        runner_temp = Path(self.runtime_root.name)
        producer_output = runner_temp / "animemo-release-producer-output"
        qualification_output = (
            runner_temp / "animemo-release-qualification-output"
        )
        for output in (producer_output, qualification_output):
            output.mkdir(mode=0o700, exist_ok=True)
            output.chmod(0o700)
        session_root = runner_temp / (
            "animemo-release-producer-session." + secrets.token_hex(5)
        )
        session_root.mkdir(mode=0o700)
        session_root.chmod(0o700)
        state = {
            "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT": str(session_root),
            "HOME": str(session_root / "home"),
            "XDG_CACHE_HOME": str(session_root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(session_root / "xdg-config"),
            "XDG_DATA_HOME": str(session_root / "xdg-data"),
            "XDG_STATE_HOME": str(session_root / "xdg-state"),
            "GH_CONFIG_DIR": str(session_root / "xdg-config" / "gh"),
            "GOPATH": str(session_root / "go-path"),
            "GOMODCACHE": str(session_root / "go-module-cache"),
            "GOCACHE": str(session_root / "go-build-cache"),
            "GOTMPDIR": str(session_root / "go-tmp"),
            "GOENV": "off",
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "GOPROXY": "https://proxy.golang.org,direct",
            "GOSUMDB": "sum.golang.org",
            "GOPRIVATE": "",
            "GONOSUMDB": "",
            "GONOPROXY": "",
            "GOINSECURE": "",
            "GOFLAGS": "",
        }
        for suffix in (
            "home",
            "xdg-cache",
            "xdg-config",
            "xdg-data",
            "xdg-state",
            "go-path",
            "go-module-cache",
            "go-build-cache",
            "go-tmp",
            "runtime-output",
        ):
            target = session_root / suffix
            target.mkdir(mode=0o700)
            target.chmod(0o700)
        if poison_relative is not None:
            poison = session_root / poison_relative
            poison.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            poison.write_text("poison", encoding="utf-8")
        if linked_relative is not None:
            linked = session_root / linked_relative
            linked.rmdir()
            authority = runner_temp / (
                "linked-state-authority-" + secrets.token_hex(5)
            )
            authority.mkdir(mode=0o700)
            authority.chmod(0o700)
            linked.symlink_to(authority, target_is_directory=True)
        if session_mode is not None:
            session_root.chmod(session_mode)
        if symlink_session:
            linked_session = runner_temp / (
                "animemo-release-producer-session." + secrets.token_hex(5)
            )
            linked_session.symlink_to(session_root, target_is_directory=True)
            state["ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT"] = str(linked_session)
        if state_overrides:
            state.update(state_overrides)
        environment_arguments: list[str] = []
        for name, value in state.items():
            environment_arguments.extend(("--env", f"{name}={value}"))
        mount_arguments: list[str] = []
        for mount in extra_mounts:
            mount_arguments.extend(("--mount", mount))
        return _completed(
            [
                docker,
                "run",
                "--rm",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,mode=1777",
                "--user",
                container_user or f"{os.getuid()}:{os.getgid()}",
                "--mount",
                f"type=bind,src={workspace},dst={workspace}",
                "--mount",
                f"type=bind,src={runner_temp},dst={runner_temp}",
                "--mount",
                "type=bind,src="
                f"{producer_output},dst={workspace / 'release-output'}",
                "--mount",
                "type=bind,src="
                f"{qualification_output},dst={workspace / 'release-qualification'}",
                *mount_arguments,
                "--workdir",
                str(workspace),
                "--env",
                f"GITHUB_WORKSPACE={workspace}",
                "--env",
                f"RUNNER_TEMP={runner_temp}",
                *environment_arguments,
                "--env",
                f"PYTHONSAFEPATH={pythonsafepath}",
                "--env",
                "PYTHONNOUSERSITE=1",
                "--env",
                f"PYTHONPATH={pythonpath}",
                self.image_id,
                "true",
            ],
            timeout=120,
        )

    def test_real_exact_image_runs_release_cli_from_the_mounted_workspace(self) -> None:
        help_result = self._wrapper("python", "-P", "-B", "-m", "release.cli", "--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr[-2000:])

        provenance = self._wrapper(
            "python",
            "-P",
            "-B",
            "-c",
            "import importlib, json, os, sys; from pathlib import Path; "
            "names=('release.cli','release.producer_toolchain',"
            "'scripts.formal_windows_pretrust','scripts.release_authority'); "
            "origins={name:str(Path(importlib.import_module(name).__file__).resolve()) "
            "for name in names}; workspace=Path(os.environ['GITHUB_WORKSPACE']).resolve(); "
            "print(json.dumps({'noUserSite':os.environ['PYTHONNOUSERSITE'],"
            "'origins':origins,'pythonPath':os.environ['PYTHONPATH'],"
            "'safePath':os.environ['PYTHONSAFEPATH'],'workspaceCount':sum("
            "1 for item in sys.path if item and Path(item).resolve()==workspace)},"
            "sort_keys=True,separators=(',',':')))",
            cwd=Path("/"),
        )
        self.assertEqual(provenance.returncode, 0, provenance.stderr[-2000:])
        actual = json.loads(provenance.stdout)
        self.assertEqual(
            actual,
            {
                "noUserSite": "1",
                "origins": {
                    "release.cli": str((ROOT / "release" / "cli.py").resolve()),
                    "release.producer_toolchain": str(
                        (ROOT / "release" / "producer_toolchain.py").resolve()
                    ),
                    "scripts.formal_windows_pretrust": str(
                        (ROOT / "scripts" / "formal_windows_pretrust.py").resolve()
                    ),
                    "scripts.release_authority": str(
                        (ROOT / "scripts" / "release_authority.py").resolve()
                    ),
                },
                "pythonPath": str(ROOT),
                "safePath": "1",
                "workspaceCount": 1,
            },
        )
        print(
            "REAL_PRODUCER_IMPORT_RESULT="
            + json.dumps(
                {
                    "exitCode": provenance.returncode,
                    "imageId": self.image_id,
                    "moduleOrigins": actual["origins"],
                    "workspaceImportRootCount": actual["workspaceCount"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def test_real_wrapper_discards_an_existing_shadow_package_and_hostile_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="animemo-shadow-package-") as temporary:
            shadow = Path(temporary)
            package = shadow / "release"
            package.mkdir()
            (package / "__init__.py").write_text(
                "raise RuntimeError('SHADOW_SENTINEL_EXECUTED')\n",
                encoding="utf-8",
            )
            (package / "cli.py").write_text(
                "raise RuntimeError('SHADOW_SENTINEL_EXECUTED')\n",
                encoding="utf-8",
            )
            result = self._wrapper(
                "python",
                "-P",
                "-B",
                "-c",
                "from pathlib import Path; import release.cli as m; "
                "print(Path(m.__file__).resolve())",
                cwd=Path("/"),
                ambient_pythonpath=str(shadow),
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(
            result.stdout.strip(), str((ROOT / "release" / "cli.py").resolve())
        )
        self.assertNotIn("SHADOW_SENTINEL_EXECUTED", result.stdout + result.stderr)

    def test_real_wrapper_accepts_a_canonical_workspace_with_spaces(self) -> None:
        assert self.runtime_root is not None
        with tempfile.TemporaryDirectory(
            prefix="animemo-spaced-workspace-parent-"
        ) as temporary:
            workspace = Path(temporary) / "workspace with spaces"
            workspace.mkdir()
            for directory in (
                "release",
                "scripts",
                "updater",
                "durability",
                "installer",
                "deploy",
            ):
                shutil.copytree(ROOT / directory, workspace / directory)
            module = workspace / "release" / "release_attestation_verifier"
            module_hashes = (
                hashlib.sha256((module / "go.mod").read_bytes()).hexdigest(),
                hashlib.sha256((module / "go.sum").read_bytes()).hexdigest(),
            )
            producer_output = (
                Path(self.runtime_root.name) / "animemo-release-producer-output"
            )
            producer_output.mkdir(mode=0o700, exist_ok=True)
            producer_output.chmod(0o700)
            formal_parent = producer_output / ".formal-pretrust-work"
            self.assertFalse(formal_parent.exists())
            formal_parent.mkdir(mode=0o700)
            formal_parent.chmod(0o700)
            try:
                result = _completed(
                    [
                        "bash",
                        str(
                            workspace
                            / "scripts"
                            / "run-in-release-producer.sh"
                        ),
                        "--",
                        "bash",
                        "scripts/release-producer-runtime-readiness.sh",
                        "build-attestation-verifier",
                    ],
                    cwd=workspace,
                    env={
                        **os.environ,
                        "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": self.image_id,
                        "GITHUB_WORKSPACE": str(workspace),
                        "RUNNER_TEMP": self.runtime_root.name,
                        "GOPATH": "/tmp/ambient-go-path",
                        "GOMODCACHE": "/tmp/ambient-go-module-cache",
                        "GOCACHE": "/tmp/ambient-go-build-cache",
                    },
                    timeout=900,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertEqual(
                    result.stdout.strip(),
                    "release producer runtime readiness PASS",
                )
                self.assertTrue((module / "offline-release-verifier").is_file())
                self.assertGreater(
                    (module / "offline-release-verifier").stat().st_size, 0
                )
                windows_verifier = formal_parent / "formal-release-verifier.exe"
                self.assertTrue(windows_verifier.is_file())
                self.assertGreater(windows_verifier.stat().st_size, 0)
                self.assertEqual(
                    (
                        hashlib.sha256((module / "go.mod").read_bytes()).hexdigest(),
                        hashlib.sha256((module / "go.sum").read_bytes()).hexdigest(),
                    ),
                    module_hashes,
                )
            finally:
                shutil.rmtree(formal_parent)

    def test_real_entrypoint_rejects_wrong_duplicate_and_disabled_authority(self) -> None:
        control = self._direct_entrypoint(pythonpath=str(ROOT))
        self.assertEqual(control.returncode, 0, control.stderr[-2000:])
        for label, pythonpath, safepath in (
            ("wrong", "/tmp/SECRET_SENTINEL_wrong-root", "1"),
            ("duplicate", f"{ROOT}:/tmp/second-root", "1"),
            ("safe path disabled", str(ROOT), "0"),
        ):
            with self.subTest(label=label):
                result = self._direct_entrypoint(
                    pythonpath=pythonpath,
                    pythonsafepath=safepath,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stderr.strip(),
                    "release producer repository import authority is invalid",
                )
                self.assertNotIn("ambient-shadow-root", result.stderr)
                self.assertNotIn("SECRET_SENTINEL", result.stderr)

    def test_real_wrapper_rejects_caller_and_symlinked_workspace_authority(self) -> None:
        for name in (
            "PYTHONPATH",
            "PYTHONSAFEPATH",
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "GOPATH",
            "GOMODCACHE",
            "GOCACHE",
            "GOTMPDIR",
            "GOENV",
            "GOTOOLCHAIN",
            "GOWORK",
            "GOPROXY",
            "GOSUMDB",
            "GOPRIVATE",
            "GONOSUMDB",
            "GONOPROXY",
            "GOINSECURE",
            "GOFLAGS",
            "GOTELEMETRY",
            "GOTELEMETRYDIR",
            "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT",
        ):
            with self.subTest(name=name):
                caller = _completed(
                    ["bash", str(WRAPPER_PATH), name, "--", "true"],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": self.image_id,
                        "GITHUB_WORKSPACE": str(ROOT),
                        "RUNNER_TEMP": self.runtime_root.name,
                        name: "/tmp/attacker-controlled",
                    },
                )
                self.assertEqual(caller.returncode, 2)

        with tempfile.TemporaryDirectory(prefix="animemo-workspace-link-") as temporary:
            link = Path(temporary) / "workspace-link"
            link.symlink_to(ROOT, target_is_directory=True)
            environment = {
                **os.environ,
                "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": self.image_id,
                "GITHUB_WORKSPACE": str(link),
                "RUNNER_TEMP": self.runtime_root.name,
            }
            linked = _completed(
                ["bash", str(WRAPPER_PATH), "--", "true"],
                cwd=ROOT,
                env=environment,
            )
            self.assertNotEqual(linked.returncode, 0)

    def test_real_runtime_readiness_closes_the_go_lifecycle_and_rootfs(self) -> None:
        assert self.runtime_root is not None
        go_mod = ROOT / "release" / "release_attestation_verifier" / "go.mod"
        go_sum = ROOT / "release" / "release_attestation_verifier" / "go.sum"
        before = (hashlib.sha256(go_mod.read_bytes()).hexdigest(),
                  hashlib.sha256(go_sum.read_bytes()).hexdigest())
        existing_sessions = set(
            Path(self.runtime_root.name).glob("animemo-release-producer-session.*")
        )

        readiness = self._wrapper(
            "bash", "scripts/release-producer-runtime-readiness.sh", "check"
        )
        self.assertEqual(readiness.returncode, 0, readiness.stderr[-2000:])
        self.assertEqual(
            readiness.stdout.strip(), "release producer runtime readiness PASS"
        )
        after = (hashlib.sha256(go_mod.read_bytes()).hexdigest(),
                 hashlib.sha256(go_sum.read_bytes()).hexdigest())
        self.assertEqual(after, before)
        self.assertEqual(
            set(
                Path(self.runtime_root.name).glob(
                    "animemo-release-producer-session.*"
                )
            ),
            existing_sessions,
        )

        closed_rootfs = self._wrapper(
            "bash",
            "-ceu",
            "if mkdir /go/animemo-authority-probe 2>/dev/null; then exit 91; fi; "
            "if mkdir /root/animemo-authority-probe 2>/dev/null; then exit 92; fi",
        )
        self.assertEqual(closed_rootfs.returncode, 0, closed_rootfs.stderr[-2000:])

        arbitrary = self._wrapper(
            "bash", "scripts/release-producer-runtime-readiness.sh", "arbitrary"
        )
        self.assertEqual(arbitrary.returncode, 2)
        self.assertEqual(
            arbitrary.stderr.strip(), "release producer runtime readiness FAIL"
        )
        extra_argument = self._wrapper(
            "bash",
            "scripts/release-producer-runtime-readiness.sh",
            "check",
            "/tmp/not-an-output",
        )
        self.assertEqual(extra_argument.returncode, 2)
        self.assertEqual(
            extra_argument.stderr.strip(),
            "release producer runtime readiness FAIL",
        )

    def test_real_runtime_readiness_detects_module_input_tampering(self) -> None:
        assert self.runtime_root is not None
        with tempfile.TemporaryDirectory(
            prefix="animemo-runtime-tamper-workspace-"
        ) as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            for directory in (
                "release",
                "scripts",
                "updater",
                "durability",
                "installer",
                "deploy",
            ):
                shutil.copytree(ROOT / directory, workspace / directory)
            module = workspace / "release" / "release_attestation_verifier"
            go_mod = module / "go.mod"
            go_sum = module / "go.sum"
            before = (
                hashlib.sha256(go_mod.read_bytes()).hexdigest(),
                hashlib.sha256(go_sum.read_bytes()).hexdigest(),
            )
            existing_download_markers = set(
                Path(self.runtime_root.name).glob(
                    "animemo-release-producer-session.*/"
                    "runtime-output/download.log"
                )
            )
            process = subprocess.Popen(
                [
                    "bash",
                    str(workspace / "scripts" / "run-in-release-producer.sh"),
                    "--",
                    "bash",
                    "scripts/release-producer-runtime-readiness.sh",
                    "check",
                ],
                cwd=workspace,
                env={
                    **os.environ,
                    "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": self.image_id,
                    "GITHUB_WORKSPACE": str(workspace),
                    "RUNNER_TEMP": self.runtime_root.name,
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 120
            download_started = False
            while time.monotonic() < deadline:
                current_download_markers = set(
                    Path(self.runtime_root.name).glob(
                        "animemo-release-producer-session.*/"
                        "runtime-output/download.log"
                    )
                )
                if current_download_markers - existing_download_markers:
                    download_started = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            if not download_started:
                process.kill()
                stdout, stderr = process.communicate(timeout=30)
                self.fail(
                    "runtime download did not expose its post-snapshot marker: "
                    + (stdout + stderr)[-2000:]
                )
            with go_mod.open("ab") as handle:
                handle.write(b"\n")
            with go_sum.open("ab") as handle:
                handle.write(b"\n")
            stdout, stderr = process.communicate(timeout=900)
            self.assertEqual(process.returncode, 2, stderr[-2000:])
            self.assertEqual(stdout, "")
            self.assertEqual(
                stderr.strip(), "release producer runtime readiness FAIL"
            )
            self.assertNotEqual(
                (
                    hashlib.sha256(go_mod.read_bytes()).hexdigest(),
                    hashlib.sha256(go_sum.read_bytes()).hexdigest(),
                ),
                before,
            )

    def test_real_entrypoint_rejects_go_state_supply_and_poisoning(self) -> None:
        control = self._direct_entrypoint(pythonpath=str(ROOT))
        self.assertEqual(control.returncode, 0, control.stderr[-2000:])

        supply_mutations = (
            ("GOENV", "/tmp/ambient-go-env"),
            ("GOTOOLCHAIN", "auto"),
            ("GOWORK", "/tmp/ambient.go.work"),
            ("GOPROXY", "off"),
            ("GOSUMDB", "off"),
            ("GOPRIVATE", "private.invalid"),
            ("GONOSUMDB", "private.invalid"),
            ("GONOPROXY", "private.invalid"),
            ("GOINSECURE", "private.invalid"),
            ("GOFLAGS", "-mod=mod"),
            ("GOTELEMETRY", "off"),
            ("GOTELEMETRYDIR", "/tmp/ambient-telemetry"),
        )
        for name, value in supply_mutations:
            with self.subTest(name=name):
                result = self._direct_entrypoint(
                    pythonpath=str(ROOT), state_overrides={name: value}
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stderr.strip(),
                    "release producer Go supply-chain environment is invalid",
                )

        for label, options in (
            ("global GOPATH", {"state_overrides": {"GOPATH": "/go"}}),
            (
                "workspace cache",
                {"state_overrides": {"GOCACHE": str(ROOT / ".go-cache")}},
            ),
            (
                "multiple cache roots",
                {"state_overrides": {"GOMODCACHE": "/tmp/one:/tmp/two"}},
            ),
            ("poisoned cache", {"poison_relative": "go-build-cache/poison"}),
            (
                "damaged module cache",
                {"poison_relative": "go-module-cache/damaged-module"},
            ),
            ("linked cache", {"linked_relative": "go-module-cache"}),
            ("extra scratch", {"poison_relative": "unexpected-scratch"}),
            ("wrong Go path", {"state_overrides": {"PATH": "/usr/bin:/bin"}}),
        ):
            with self.subTest(label=label):
                result = self._direct_entrypoint(
                    pythonpath=str(ROOT), **options
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stderr.strip(),
                    "release producer Go writable state is invalid",
                )

        for label, options in (
            (
                "wrong session parent",
                {
                    "state_overrides": {
                        "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT": self.runtime_root.name
                    }
                },
            ),
            ("linked session", {"symlink_session": True}),
            ("wrong session mode", {"session_mode": 0o755}),
        ):
            with self.subTest(label=label):
                result = self._direct_entrypoint(
                    pythonpath=str(ROOT), **options
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stderr.strip(),
                    "release producer Go session authority is invalid",
                )

        with tempfile.TemporaryDirectory(
            prefix="animemo-wrong-owner-workspace-"
        ) as temporary:
            workspace = Path(temporary)
            workspace.chmod(0o755)
            runtime_root = Path(self.runtime_root.name)
            runtime_root.chmod(0o711)
            try:
                wrong_owner = self._direct_entrypoint(
                    pythonpath=str(workspace),
                    workspace=workspace,
                    container_user=(
                        "65534:65534"
                        if os.getuid() != 65534
                        else "65533:65533"
                    ),
                )
            finally:
                runtime_root.chmod(0o700)
            self.assertEqual(wrong_owner.returncode, 2)
            self.assertEqual(
                wrong_owner.stderr.strip(),
                "release producer Go session authority is invalid",
            )

        with tempfile.TemporaryDirectory(
            prefix="animemo-wrong-go-version-"
        ) as temporary:
            fake_go = Path(temporary) / "go"
            fake_go.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = telemetry ] && [ \"${2:-}\" = off ]; then\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"${1:-}\" = version ]; then\n"
                "  printf '%s\\n' 'go version go1.26.5 linux/amd64'\n"
                "  exit 0\n"
                "fi\n"
                "exit 97\n",
                encoding="utf-8",
            )
            fake_go.chmod(0o755)
            wrong_version = self._direct_entrypoint(
                pythonpath=str(ROOT),
                extra_mounts=(
                    "type=bind,src="
                    f"{fake_go},dst=/usr/local/go/bin/go,readonly",
                ),
            )
            self.assertEqual(wrong_version.returncode, 2)
            self.assertEqual(
                wrong_version.stderr.strip(),
                "release producer Go writable state is invalid",
            )

    def test_real_concurrent_wrappers_do_not_share_go_state(self) -> None:
        assert self.runtime_root is not None
        environment = {
            **os.environ,
            "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": self.image_id,
            "GITHUB_WORKSPACE": str(ROOT),
            "RUNNER_TEMP": self.runtime_root.name,
        }
        processes = []
        for label, exit_code in (("a", 0), ("b", 37)):
            command = (
                "test -z \"$(find -P \"$GOMODCACHE\" -mindepth 1 "
                "-print -quit)\"; "
                "test -z \"$(find -P \"$GOCACHE\" -mindepth 1 "
                "-print -quit)\"; "
                "bash scripts/release-producer-runtime-readiness.sh check; "
                "test -n \"$(find -P \"$GOMODCACHE\" -mindepth 1 "
                "-print -quit)\"; "
                "test -n \"$(find -P \"$GOCACHE\" -mindepth 1 "
                "-print -quit)\"; "
                "printf '%s\\n' \"$ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT\" "
                "\"$GOPATH\" \"$GOMODCACHE\" \"$GOCACHE\" \"$GOTMPDIR\" "
                f"> \"$RUNNER_TEMP/concurrent-{label}.txt\"; "
                f"exit {exit_code}"
            )
            processes.append(
                (
                    label,
                    exit_code,
                    subprocess.Popen(
                        [
                            "bash",
                            str(WRAPPER_PATH),
                            "--",
                            "bash",
                            "-ceu",
                            command,
                        ],
                        cwd=ROOT,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    ),
                )
            )

        observed: dict[str, list[str]] = {}
        for label, expected_code, process in processes:
            stdout, stderr = process.communicate(timeout=900)
            self.assertEqual(process.returncode, expected_code, stderr[-2000:])
            self.assertEqual(
                stdout.strip(), "release producer runtime readiness PASS"
            )
            observed[label] = (
                Path(self.runtime_root.name) / f"concurrent-{label}.txt"
            ).read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(observed["a"]), 5)
        self.assertEqual(len(observed["b"]), 5)
        self.assertNotEqual(observed["a"][0], observed["b"][0])
        for values in observed.values():
            session = values[0]
            self.assertEqual(
                values[1:],
                [
                    f"{session}/go-path",
                    f"{session}/go-module-cache",
                    f"{session}/go-build-cache",
                    f"{session}/go-tmp",
                ],
            )
            self.assertFalse(Path(session).exists())

    def test_real_entrypoint_rejects_linked_critical_package_and_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="animemo-package-link-") as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            authority = workspace / ".authority"
            authority.mkdir()
            shutil.copytree(ROOT / "release", authority / "release")
            (workspace / "release").symlink_to(
                authority / "release", target_is_directory=True
            )
            package_link = self._direct_entrypoint(
                pythonpath=str(workspace), workspace=workspace
            )
            self.assertEqual(package_link.returncode, 2)
            self.assertEqual(
                package_link.stderr.strip(),
                "release producer Go module authority is invalid",
            )

        with tempfile.TemporaryDirectory(prefix="animemo-entry-link-") as temporary:
            workspace = Path(temporary) / "workspace"
            release = workspace / "release"
            scripts = workspace / "scripts"
            authority = workspace / ".authority"
            release.mkdir(parents=True)
            scripts.mkdir()
            authority.mkdir()
            for name in ("updater", "durability", "installer"):
                (workspace / name).mkdir()
            for relative in (
                "__init__.py",
                "producer_toolchain.py",
                "requirements.lock",
            ):
                shutil.copy2(ROOT / "release" / relative, release / relative)
            shutil.copytree(
                ROOT / "release" / "release_attestation_verifier",
                release / "release_attestation_verifier",
            )
            shutil.copy2(ROOT / "release" / "cli.py", authority / "cli.py")
            (release / "cli.py").symlink_to(authority / "cli.py")
            entry_link = self._direct_entrypoint(
                pythonpath=str(workspace), workspace=workspace
            )
            self.assertEqual(entry_link.returncode, 2)
            self.assertEqual(
                entry_link.stderr.strip(),
                "release producer repository import authority is invalid",
            )

    def test_real_entrypoint_rejects_python_startup_and_stdlib_shadow_entries(self) -> None:
        fixtures = (
            ("sitecustomize.py", False),
            ("sitecustomize", True),
            ("sitecustomize.pyc", False),
            ("sitecustomize.so", False),
            ("pathlib.py", False),
            ("pathlib.pyc", False),
            ("pathlib.so", False),
            ("pathlib", True),
            ("jsonschema", True),
            ("packaging", True),
        )
        for shadow_name, is_package in fixtures:
            with self.subTest(shadow_name=shadow_name), tempfile.TemporaryDirectory(
                prefix="animemo-startup-shadow-"
            ) as temporary:
                workspace = Path(temporary) / "workspace"
                workspace.mkdir()
                for directory in ("release", "scripts", "updater", "durability", "installer"):
                    shutil.copytree(ROOT / directory, workspace / directory)
                (workspace / "deploy").mkdir()
                shutil.copy2(
                    ROOT / "deploy" / "release-producer.Dockerfile",
                    workspace / "deploy" / "release-producer.Dockerfile",
                )
                shadow = workspace / shadow_name
                if is_package:
                    shadow.mkdir()
                    shadow = shadow / "__init__.py"
                shadow.write_text(
                    "print('STARTUP_SHADOW_SENTINEL_EXECUTED')\n",
                    encoding="utf-8",
                )
                result = self._direct_entrypoint(
                    pythonpath=str(workspace), workspace=workspace
                )
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(
                    "STARTUP_SHADOW_SENTINEL_EXECUTED",
                    result.stdout + result.stderr,
                )
                self.assertEqual(
                    result.stderr.strip(),
                    "release producer repository import authority is invalid",
                )

    def test_real_wrapper_rejects_a_symlinked_launcher_with_a_fake_workspace(self) -> None:
        assert self.runtime_root is not None
        with tempfile.TemporaryDirectory(prefix="animemo-linked-wrapper-") as temporary:
            fake_workspace = Path(temporary) / "workspace"
            fake_scripts = fake_workspace / "scripts"
            fake_scripts.mkdir(parents=True)
            linked_wrapper = fake_scripts / "run-in-release-producer.sh"
            linked_wrapper.symlink_to(WRAPPER_PATH)
            result = _completed(
                ["bash", str(linked_wrapper), "--", "true"],
                cwd=Path("/"),
                env={
                    **os.environ,
                    "ANIMEMO_RELEASE_PRODUCER_IMAGE_ID": self.image_id,
                    "GITHUB_WORKSPACE": str(fake_workspace),
                    "RUNNER_TEMP": self.runtime_root.name,
                },
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            result.stderr.strip(),
            "release producer mount authority is not canonical",
        )
