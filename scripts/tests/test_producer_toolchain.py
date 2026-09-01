from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def _completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=kwargs.pop("timeout", 120),
        **kwargs,
    )


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

        self.assertIn(
            'producer_gotmp="$RUNNER_TEMP/animemo-release-producer-gotmp"',
            wrapper,
        )
        self.assertIn(
            'install -d -m 0700 "$producer_home" "$producer_gotmp"',
            wrapper,
        )
        self.assertIn(
            "--tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777",
            wrapper,
        )
        self.assertIn('--env "GOTMPDIR=$producer_gotmp"', wrapper)
        self.assertNotIn("|GOTMPDIR|", wrapper)
        self.assertIn(
            'expected_gotmp="$RUNNER_TEMP/animemo-release-producer-gotmp"',
            entrypoint,
        )
        self.assertIn('"$GOTMPDIR" != "$expected_gotmp"', entrypoint)
        self.assertIn('! -d "$GOTMPDIR"', entrypoint)
        self.assertIn('-L "$GOTMPDIR"', entrypoint)
        self.assertIn('! -O "$GOTMPDIR"', entrypoint)
        self.assertIn('stat -c \'%a\' "$GOTMPDIR"', entrypoint)

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
            }
        )
        return _completed(
            ["bash", str(WRAPPER_PATH), "--", *command],
            cwd=cwd or ROOT,
            env=environment,
            timeout=180,
        )

    def _direct_entrypoint(
        self,
        *,
        pythonpath: str,
        pythonsafepath: str = "1",
        workspace: Path = ROOT,
    ) -> subprocess.CompletedProcess[str]:
        assert self.runtime_root is not None
        docker = shutil.which("docker")
        assert docker is not None
        runner_temp = Path(self.runtime_root.name)
        gotmp = runner_temp / "animemo-release-producer-gotmp"
        gotmp.mkdir(mode=0o700, exist_ok=True)
        gotmp.chmod(0o700)
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
                f"{os.getuid()}:{os.getgid()}",
                "--mount",
                f"type=bind,src={workspace},dst={workspace}",
                "--mount",
                f"type=bind,src={runner_temp},dst={runner_temp}",
                "--workdir",
                str(workspace),
                "--env",
                f"GITHUB_WORKSPACE={workspace}",
                "--env",
                f"RUNNER_TEMP={runner_temp}",
                "--env",
                f"GOTMPDIR={gotmp}",
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
        for name in ("PYTHONPATH", "PYTHONSAFEPATH"):
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
                "release producer repository import authority is invalid",
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
