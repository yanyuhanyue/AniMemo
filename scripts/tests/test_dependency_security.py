from __future__ import annotations

import base64
import json
import re
import unittest
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def _hashed_requirements(path: Path) -> list[str]:
    logical: list[str] = []
    pending = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].strip()
            continue
        logical.append(pending)
        pending = ""
    if pending:
        logical.append(pending)
    return logical


class DependencySecurityContractTests(unittest.TestCase):
    def test_sqlparse_pin_excludes_known_vulnerable_release_line(self) -> None:
        requirements = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
        pins = {
            line.split("==", 1)[0].strip(): line.split("==", 1)[1].strip()
            for line in requirements.splitlines()
            if "==" in line and not line.lstrip().startswith("#")
        }
        self.assertEqual(pins.get("sqlparse"), "0.6.0")

    def test_dependency_toolchain_uses_patched_exact_pip_line(self) -> None:
        requirements = (ROOT / "scripts" / "requirements-tools.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("pip==26.2.1", requirements.splitlines())
        self.assertIn("pip-tools==7.6.1", requirements.splitlines())
        self.assertNotIn("pip<26", requirements.splitlines())

    def test_runtime_lockfiles_are_exact_and_sha256_complete(self) -> None:
        lockfiles = (
            "backend/pip-bootstrap.lock",
            "backend/container-requirements.lock",
            "backend/requirements.lock",
            "release/requirements.lock",
            "durability/requirements.lock",
            "scripts/requirements-tools.lock",
            "bridges/astrbot-runtime.requirements.lock",
            "bridges/astrbot_plugin_animemo_bridge/requirements.lock",
        )
        exact = re.compile(
            r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?"
            r"==[^\s;]+(?:\s*;\s*.*?)?\s+"
            r"(?:--hash=sha256:[0-9a-f]{64}\s*)+$"
        )
        for relative in lockfiles:
            requirements = _hashed_requirements(ROOT / relative)
            self.assertTrue(requirements, relative)
            for requirement in requirements:
                self.assertRegex(requirement, exact, f"{relative}: {requirement}")

    def test_workflow_dependency_consumers_require_hashes(self) -> None:
        offenders: list[str] = []
        consumers = [
            *sorted((ROOT / ".github" / "workflows").glob("*.y*ml")),
            *sorted((ROOT / "deploy").glob("*.Dockerfile")),
        ]
        for consumer in consumers:
            source = consumer.read_text(encoding="utf-8").replace("\\\n", " ")
            for line_number, line in enumerate(source.splitlines(), start=1):
                if not re.search(r"python(?:3)? -m pip (?:install|download)\b", line):
                    continue
                if "-r " in line and (
                    "--require-hashes" not in line or ".lock" not in line
                ):
                    offenders.append(f"{consumer.name}:{line_number}:{line.strip()}")
        self.assertEqual(offenders, [])

    def test_astrbot_runtime_uses_immutable_sources_and_verified_hashed_lock(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        references = re.findall(
            r"repository:\s*AstrBotDevs/AstrBot\s*\n\s*ref:\s*([0-9a-f]{40})",
            workflow,
        )
        self.assertEqual(len(references), 2)
        self.assertEqual(len(set(references)), 2)
        self.assertNotIn("ref: ${{ matrix.", workflow)
        self.assertIn(
            "6b5b28e189a16b8a0db4f177e32d14e39073c3e9b62ff25f9dc3515b1e232804  .astrbot-runtime/requirements.txt",
            workflow,
        )
        self.assertIn("astrbot-upstream-requirements.txt", workflow)
        self.assertIn("astrbot-authority-requirements.txt", workflow)
        self.assertIn("cmp \"$RUNNER_TEMP/astrbot-upstream-requirements.txt\"", workflow)
        self.assertIn("-r scripts/requirements-tools.lock", workflow)
        self.assertIn(
            "python -m pip install --no-build-isolation --require-hashes",
            workflow,
        )
        build_authority = " ".join(
            _hashed_requirements(ROOT / "scripts" / "requirements-tools.lock")
        )
        for package in ("build==", "pip==", "setuptools==", "wheel=="):
            self.assertIn(package, build_authority)
        runtime_lock = (
            ROOT / "bridges" / "astrbot-runtime.requirements.lock"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'pywin32==312 ; sys_platform == "win32"',
            runtime_lock,
        )

    def test_plugin_bind_mounts_are_owned_by_the_runtime_identity(self) -> None:
        sources = {
            ".github/workflows/release-gate.yml": 'install -d -m 0755 -o 10001 -g 10001 "$data_root/plugins"',
            ".github/workflows/performance.yml": 'install -d -m 0755 -o 10001 -g 10001 "$data_root/plugins"',
            "scripts/rehearse-release-images.sh": 'install -d -m 0755 -o 10001 -g 10001 "$DATA_ROOT/plugins"',
            "scripts/dr-rehearsal.sh": 'install -d -m 0755 -o 10001 -g 10001 "$DATA_A/plugins"',
            "scripts/stateful-upgrade-gate.sh": 'install -d -m 0755 -o 10001 -g 10001 "$DATA_ROOT/plugins"',
        }
        for relative, authority in sources.items():
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(authority, source)
        dr_source = (ROOT / "scripts" / "dr-rehearsal.sh").read_text(encoding="utf-8")
        self.assertIn('chown -R 10001:10001 "$DATA_B/plugins"', dr_source)
        self.assertNotIn('chmod -R a+rwx "$DATA_B/plugins"', dr_source)

        backend_dockerfile = (ROOT / "deploy" / "backend.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("addgroup -S -g 10001 animemo", backend_dockerfile)
        self.assertIn(
            "adduser -S -D -u 10001 -G animemo -h /home/animemo animemo",
            backend_dockerfile,
        )
        self.assertIn("USER 10001:10001", backend_dockerfile)

    def test_backend_installer_modules_are_immutable_but_runtime_importable(self) -> None:
        backend = (ROOT / "deploy" / "backend.Dockerfile").read_text(encoding="utf-8")
        for module in ("__init__.py", "safe_archive.py"):
            self.assertIn(
                f"COPY --chown=0:0 --chmod=0444 installer/{module} "
                f"/app/installer/{module}",
                backend,
            )
        self.assertIn("chown root:root /app/installer", backend)
        self.assertIn("chmod 0555 /app/installer", backend)
        self.assertNotRegex(
            backend,
            r"chown(?:\s+-R)?\s+(?:animemo:animemo|10001:10001)\s+/app/installer",
        )

    def test_development_bootstrap_exposes_the_shared_archive_package(self) -> None:
        bash_source = (ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
        powershell_source = (ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
        bash_assignments = [
            line.strip()
            for line in bash_source.splitlines()
            if line.strip().startswith("export PYTHONPATH=")
        ]
        powershell_assignments = [
            line.strip()
            for line in powershell_source.splitlines()
            if line.strip().casefold().startswith("$env:pythonpath")
        ]
        self.assertEqual(bash_assignments, ['export PYTHONPATH="$ROOT"'])
        self.assertEqual(powershell_assignments, ["$env:PYTHONPATH = $root"])

    def test_development_plugin_storage_is_separate_from_tracked_plugin_sources(self) -> None:
        settings_source = (ROOT / "backend" / "config" / "settings.py").read_text(
            encoding="utf-8"
        )
        development_environment = (
            ROOT / ".env.development.example"
        ).read_text(encoding="utf-8").splitlines()
        environment_documentation = (ROOT / ".env.example").read_text(
            encoding="utf-8"
        )
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn('else BASE_DIR.parent / "plugins"', settings_source)
        self.assertIn("_plugin_root_candidate.is_absolute()", settings_source)
        self.assertIn("BASE_DIR.parent / _plugin_root_candidate", settings_source)
        self.assertIn("PLUGIN_ROOT=runtime/plugins", development_environment)
        self.assertIn("新配置不得指向仓库内的 plugins 源码目录", environment_documentation)
        self.assertIn("仅为兼容既有本地 .env 与 CAS 数据", environment_documentation)
        self.assertIn("/runtime/plugins/", gitignore)

    def test_backend_runtime_and_python_tooling_are_environment_isolated(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        backend = workflow[
            workflow.index("  backend:\n") : workflow.index(
                "  bootstrap-smoke:\n"
            )
        ]
        self.assertIn('tools_runtime="$RUNNER_TEMP/animemo-python-tools"', backend)
        self.assertIn('python -m venv "$tools_runtime"', backend)
        self.assertIn(
            '"$tools_runtime/bin/python" -m pip install --require-hashes',
            backend,
        )
        self.assertNotIn(
            "python -m pip install --require-hashes -r scripts/requirements-tools.lock",
            backend,
        )

    def test_astrbot_runtime_and_build_authority_pins_are_compatible(self) -> None:
        locks = (
            ROOT / "scripts" / "requirements-tools.lock",
            ROOT / "bridges" / "astrbot-runtime.requirements.lock",
        )
        versions: dict[str, set[str]] = {}
        for lock in locks:
            for requirement in _hashed_requirements(lock):
                match = re.match(
                    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
                    r"(?:\[[^]]+\])?==(?P<version>[^\s;]+)",
                    requirement,
                )
                self.assertIsNotNone(match, requirement)
                name = match.group("name").lower().replace("_", "-")
                versions.setdefault(name, set()).add(match.group("version"))
        conflicts = {
            name: sorted(values)
            for name, values in versions.items()
            if len(values) != 1
        }
        self.assertEqual(conflicts, {})

    def test_astrbot_linux_lock_satisfies_every_active_upstream_requirement(self) -> None:
        runtime_lock = _hashed_requirements(
            ROOT / "bridges" / "astrbot-runtime.requirements.lock"
        )
        pins: dict[str, Version] = {}
        for requirement in runtime_lock:
            match = re.match(
                r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
                r"(?:\[[^]]+\])?==(?P<version>[^\s;]+)",
                requirement,
            )
            self.assertIsNotNone(match, requirement)
            pins[match.group("name").lower().replace("_", "-")] = Version(
                match.group("version")
            )

        environment = default_environment()
        environment.update(
            {
                "platform_system": "Linux",
                "python_full_version": "3.12.10",
                "python_version": "3.12",
                "sys_platform": "linux",
            }
        )
        missing: list[str] = []
        incompatible: list[str] = []
        for raw_line in (
            ROOT / "bridges" / "astrbot-runtime.requirements.in"
        ).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            name = requirement.name.lower().replace("_", "-")
            version = pins.get(name)
            if version is None:
                missing.append(line)
            elif version not in requirement.specifier:
                incompatible.append(f"{line} -> {version}")
        self.assertEqual(missing, [])
        self.assertEqual(incompatible, [])

    def test_plugin_filesystem_consumers_use_the_shared_archive_boundary(self) -> None:
        installer = (ROOT / "backend" / "plugin_host" / "installer.py").read_text(
            encoding="utf-8"
        )
        services = (ROOT / "backend" / "plugin_host" / "services.py").read_text(
            encoding="utf-8"
        )
        extract = installer[
            installer.index("    def _extract(") : installer.index(
                "    def _assert_growth_allowed"
            )
        ]
        preview = services[
            services.index("def create_frontend_preview(") : services.index(
                "def _payload_for_version("
            )
        ]
        scan = services[
            services.index("def static_security_scan(") : services.index(
                "def store_package_blob("
            )
        ]
        for source in (extract, preview, scan):
            self.assertIn("validated_package_members", source)
            self.assertIn("read_validated_package_member", source)
            self.assertNotIn("archive.read", source)
        self.assertIn("_read_verified_cas_blob", preview)
        submission = services[
            services.index("def _payload_for_version(") : services.index(
                "def submit_version("
            )
        ]
        self.assertIn("_read_verified_cas_blob", submission)
        self.assertNotIn("read_bytes", submission)

    def test_external_github_actions_are_pinned_to_exact_commits(self) -> None:
        mutable: list[str] = []
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
            source = workflow.read_text(encoding="utf-8")
            for match in re.finditer(r"\buses:\s*([^\s#]+)", source):
                reference = match.group(1)
                if reference.startswith("./"):
                    continue
                if not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference):
                    mutable.append(f"{workflow.name}:{reference}")
        self.assertEqual(mutable, [])

    def test_release_container_base_images_are_pinned_to_exact_digests(self) -> None:
        mutable: list[str] = []
        for dockerfile in sorted((ROOT / "deploy").glob("*.Dockerfile")):
            for line in dockerfile.read_text(encoding="utf-8").splitlines():
                if not line.startswith("FROM "):
                    continue
                reference = line.split()[1]
                if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", reference):
                    mutable.append(f"{dockerfile.name}:{reference}")
        self.assertEqual(mutable, [])

        backend = (ROOT / "deploy" / "backend.Dockerfile").read_text(encoding="utf-8")
        frontend = (ROOT / "deploy" / "frontend.Dockerfile").read_text(encoding="utf-8")
        nginx_entrypoint = (ROOT / "deploy" / "nginx-entrypoint.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "FROM python:3.12-alpine@sha256:"
            "d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31",
            backend,
        )
        self.assertIn(
            "FROM node:24-alpine@sha256:"
            "d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43",
            frontend,
        )
        self.assertIn(
            "FROM nginx:1.29-alpine@sha256:"
            "5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de",
            frontend,
        )
        self.assertIn(
            "RUN python -m pip install --no-cache-dir --no-deps --require-hashes",
            backend,
        )
        install_requirements = (
            "RUN python -m pip install --no-cache-dir --require-hashes \\\n"
            "      -r /app/requirements.lock"
        )
        remove_runtime_pip = "RUN python -m pip uninstall --yes pip"
        self.assertIn(install_requirements, backend)
        self.assertIn(remove_runtime_pip, backend)
        self.assertLess(backend.index(install_requirements), backend.index(remove_runtime_pip))
        self.assertLess(
            backend.index(remove_runtime_pip),
            backend.index("USER 10001:10001"),
        )
        self.assertIn("npm pack npm@12.0.2 --ignore-scripts", frontend)
        self.assertIn("sha512sum -c /tmp/npm-12.0.2.sha512", frontend)
        self.assertIn(
            'ENTRYPOINT ["/usr/local/bin/animemo-nginx-entrypoint"]', frontend
        )
        self.assertIn('CMD ["nginx", "-g", "daemon off;"]', frontend)
        self.assertIn("/proc/net/route", nginx_entrypoint)
        self.assertIn("for (octet = 1; octet <= 4; octet += 1)", nginx_entrypoint)
        self.assertNotIn("for (index =", nginx_entrypoint)
        self.assertIn('${gateway}/32', nginx_entrypoint)
        self.assertNotRegex(backend, r"\bapk\s+(?:add|upgrade)\b")
        self.assertNotRegex(frontend, r"\bapk\s+(?:add|upgrade)\b")
        self.assertIn("PYTHONPATH=/app", backend)
        self.assertIn(
            "COPY --chown=0:0 --chmod=0444 installer/__init__.py "
            "/app/installer/__init__.py",
            backend,
        )
        self.assertIn(
            "COPY --chown=0:0 --chmod=0444 installer/safe_archive.py "
            "/app/installer/safe_archive.py",
            backend,
        )
        ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(
            ci_workflow.count("PYTHONPATH: ${{ github.workspace }}"), 2
        )

    def test_release_producer_toolchain_is_closed(self) -> None:
        workflow_root = ROOT / ".github" / "workflows"
        for workflow in sorted(workflow_root.glob("*.y*ml")):
            source = workflow.read_text(encoding="utf-8")
            self.assertNotIn("ubuntu-latest", source, workflow.name)
            self.assertNotIn('python-version: "3.12"', source, workflow.name)

        release_workflow = (workflow_root / "release.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", release_workflow)
        self.assertIn("python-version: \"3.12.10\"", release_workflow)
        self.assertIn("version: v0.36.0", release_workflow)
        self.assertIn(
            "image=moby/buildkit:v0.32.0@sha256:"
            "1f8167fcb0eca5b7126353d35299386945cbb8949cc516c592a49f80cfce4fa2",
            release_workflow,
        )
        self.assertIn("release-producer-toolchain-receipt.json", release_workflow)
        self.assertIn(
            "--file deploy/release-producer.Dockerfile",
            release_workflow,
        )
        preflight_start = release_workflow.index(
            "- name: Freeze and bind the complete Release Notes population"
        )
        preflight_boundary = release_workflow.find("\n      - ", preflight_start + 1)
        preflight_block = release_workflow[
            preflight_start : preflight_boundary if preflight_boundary >= 0 else None
        ]
        self.assertNotIn("scripts/run-in-release-producer.sh", preflight_block)
        self.assertIn("scripts/release_notes_snapshot.py", preflight_block)
        for step in (
            "Bind the hosted platform qualification into Installer materials",
            "Bind the exact byte-producing toolchain",
            "Prepare closed Candidate OCI roots",
            "Close and verify all four Candidate OCI layouts",
            "Generate and validate manifest, checksums, and unsigned provenance input plan",
        ):
            start = release_workflow.index(f"- name: {step}")
            boundary = release_workflow.find("\n      - ", start + 1)
            block = release_workflow[start : boundary if boundary >= 0 else None]
            self.assertIn("scripts/run-in-release-producer.sh", block, step)
        self.assertIn(
            "release-output/release-producer-toolchain-receipt.json",
            release_workflow,
        )
        self.assertNotIn(
            "release-dry-run-input/release-producer-toolchain-receipt.json",
            release_workflow,
        )
        self.assertIn("validate_producer_toolchain_receipt", release_workflow)

        toolchain = json.loads(
            (ROOT / "release" / "producer-toolchain.lock.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(toolchain["githubHostedRunner"]["observationOnly"])
        self.assertEqual(
            toolchain["byteAuthority"]["buildkit"]["version"], "v0.32.0"
        )
        producer = toolchain["byteAuthority"]["releaseProducer"]
        producer_dockerfile = (ROOT / producer["dockerfile"]).read_text(
            encoding="utf-8"
        )
        self.assertIn(producer["pythonBase"], producer_dockerfile)
        self.assertIn(producer["goBase"], producer_dockerfile)
        for field in ("githubCliSha256", "craneSha256", "jqSha256"):
            self.assertIn(producer[field], producer_dockerfile)
        wrapper = (ROOT / "scripts" / "run-in-release-producer.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--read-only", wrapper)
        self.assertIn("--cap-drop=ALL", wrapper)
        self.assertIn("--security-opt=no-new-privileges", wrapper)
        self.assertNotIn("/var/run/docker.sock", wrapper)
        entrypoint = (
            ROOT / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("release.requirements.lock", entrypoint)
        self.assertIn("release-producer.Dockerfile", entrypoint)
        npm_integrity = toolchain["byteAuthority"]["npm"]["integrity"]
        self.assertTrue(npm_integrity.startswith("sha512-"))
        npm_sha512 = base64.b64decode(npm_integrity.removeprefix("sha512-")).hex()
        frontend = (ROOT / "deploy" / "frontend.Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn(npm_sha512, frontend)

    def test_release_producer_repository_import_authority_is_fail_closed(self) -> None:
        wrapper = (ROOT / "scripts" / "run-in-release-producer.sh").read_text(
            encoding="utf-8"
        )
        entrypoint = (
            ROOT / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")
        dockerfile = (
            ROOT / "deploy" / "release-producer.Dockerfile"
        ).read_text(encoding="utf-8")

        allowlist = wrapper[wrapper.index("allowed=") : wrapper.index("environment=()")]
        self.assertNotIn("PYTHONPATH", allowlist)
        self.assertNotIn("PYTHONSAFEPATH", allowlist)
        self.assertEqual(wrapper.count('--env "PYTHONSAFEPATH=1"'), 1)
        self.assertEqual(wrapper.count('--env "PYTHONPATH=$GITHUB_WORKSPACE"'), 1)
        self.assertNotIn("${PYTHONPATH", wrapper)
        self.assertNotIn("PYTHONPATH=.", wrapper)
        for boundary in (
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            '--env "HOME=$producer_home"',
            '--env "GOTMPDIR=$producer_gotmp"',
        ):
            self.assertIn(boundary, wrapper)

        for closed_check in (
            '"${PYTHONSAFEPATH:-}" != "1"',
            '"${PYTHONPATH:-}" != "$GITHUB_WORKSPACE"',
            "pwd -P",
            "python -I -S -B",
            "importlib.util.find_spec",
            ".resolve(strict=True)",
            "sys.stdlib_module_names",
            "release.cli",
            "release.producer_toolchain",
            "scripts.formal_windows_pretrust",
            "scripts.release_authority",
        ):
            self.assertIn(closed_check, entrypoint)
        self.assertIn("importlib.import_module", entrypoint)
        self.assertIn('${BASH_SOURCE[0]}', wrapper)
        self.assertIn('realpath -e -- "${BASH_SOURCE[0]}"', wrapper)
        self.assertIn("allowed_workspace_roots", entrypoint)
        self.assertIn("unexpected workspace import root", entrypoint)

        self.assertIn("ENV LANG=C.UTF-8", dockerfile)
        self.assertIn("PYTHONSAFEPATH=1", dockerfile)
        self.assertEqual(
            [line for line in dockerfile.splitlines() if line.startswith("COPY ")],
            [
                "COPY --from=python-runtime /usr/local/ /usr/local/",
                "COPY release/requirements.lock /opt/animemo-locks/release.requirements.lock",
                "COPY deploy/release-producer.Dockerfile /opt/animemo-locks/release-producer.Dockerfile",
                "COPY scripts/release-producer-entrypoint.sh /usr/local/bin/release-producer-entrypoint",
            ],
        )
        self.assertNotRegex(dockerfile, r"pip install[^\n]*(?:-e\s+\.|\s+\.)")

    def test_release_verifier_uses_patched_go_toolchain_and_grpc(self) -> None:
        verifier = ROOT / "release" / "release_attestation_verifier"
        go_mod = (verifier / "go.mod").read_text(encoding="utf-8")
        self.assertIn("go 1.26.6", go_mod.splitlines())
        self.assertIn("google.golang.org/grpc v1.83.1", go_mod)
        self.assertNotIn("google.golang.org/grpc v1.82.1", go_mod)

        release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        dry_run = release_workflow[
            release_workflow.index("  dry-run:\n") : release_workflow.index(
                "  publish:\n"
            )
        ]
        publish = release_workflow[release_workflow.index("  publish:\n") :]
        helper = (
            ROOT / "scripts" / "release-producer-runtime-readiness.sh"
        ).read_text(encoding="utf-8")
        setup_go = "actions/setup-go@924ae3a1cded613372ab5595356fb5720e22ba16"
        linux_verifier_build = (
            "CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOPROXY=off GOSUMDB=off"
        )
        windows_verifier_build = (
            "CGO_ENABLED=0 GOOS=windows GOARCH=amd64 GOPROXY=off GOSUMDB=off"
        )

        self.assertEqual(release_workflow.count("go-version: '1.26.6'"), 1)
        self.assertEqual(dry_run.count(setup_go), 1)
        self.assertEqual(dry_run.count("go-version: '1.26.6'"), 1)
        self.assertNotIn("go mod download", dry_run)
        self.assertNotIn("go mod verify", dry_run)
        self.assertNotIn("go test ./...", dry_run)
        self.assertNotIn("go build -mod=readonly", dry_run)
        self.assertEqual(dry_run.count("release-producer-runtime-readiness.sh"), 2)
        self.assertEqual(helper.count(linux_verifier_build), 1)
        self.assertEqual(helper.count(windows_verifier_build), 1)
        self.assertEqual(helper.count("go build -mod=readonly -trimpath"), 2)
        self.assertEqual(publish.count(setup_go), 0)
        self.assertEqual(publish.count("go-version: '1.26.6'"), 0)
        self.assertEqual(publish.count(linux_verifier_build), 0)
        self.assertEqual(publish.count(windows_verifier_build), 0)
        self.assertEqual(publish.count("release-producer-runtime-readiness.sh"), 0)
        self.assertEqual(publish.count("build-initial-trust-kit"), 0)
        self.assertNotIn("go-version: '1.25.8'", release_workflow)

        contract = json.loads(
            (verifier / "INSTALLATION_CONTRACT_V2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["build"]["minimumGoVersion"], "1.26.6")

    def test_release_producer_go_state_and_supply_chain_are_closed(self) -> None:
        wrapper = (ROOT / "scripts" / "run-in-release-producer.sh").read_text(
            encoding="utf-8"
        )
        entrypoint = (
            ROOT / "scripts" / "release-producer-entrypoint.sh"
        ).read_text(encoding="utf-8")
        helper = (
            ROOT / "scripts" / "release-producer-runtime-readiness.sh"
        ).read_text(encoding="utf-8")
        dockerfile = (
            ROOT / "deploy" / "release-producer.Dockerfile"
        ).read_text(encoding="utf-8")

        for boundary in (
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "animemo-release-producer-session.XXXXXXXXXX",
            'GOENV=off',
            'GOTOOLCHAIN=local',
            'GOWORK=off',
            'GOSUMDB=sum.golang.org',
            'GOINSECURE=',
        ):
            self.assertIn(boundary, wrapper)
        self.assertNotIn('--mount "type=bind,src=/go', wrapper)
        self.assertNotIn('dst=/go', wrapper)
        self.assertNotIn("--privileged", wrapper)

        allowlist = wrapper[wrapper.index("allowed=") : wrapper.index("environment=()")]
        for forbidden in (
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
            "HOME",
            "XDG_",
            "ANIMEMO_RELEASE_PRODUCER_SESSION_ROOT",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, allowlist)

        self.assertIn("go telemetry off", entrypoint)
        self.assertIn("GOTELEMETRYDIR", entrypoint)
        self.assertIn("go version go1.26.6 linux/amd64", entrypoint)
        self.assertIn("GOPROXY=https://proxy.golang.org,direct", helper)
        self.assertIn("GOSUMDB=sum.golang.org", helper)
        self.assertIn("GOPROXY=off GOSUMDB=off", helper)
        self.assertIn("go mod verify", helper)
        self.assertIn("runtime-output", helper)
        self.assertIn("-type l", entrypoint)
        self.assertIn("animemo-release-producer-session", entrypoint)
        self.assertIn("validate_output_staging", entrypoint)
        self.assertIn("animemo-release-producer-output", entrypoint)
        self.assertIn("animemo-release-qualification-output", entrypoint)
        self.assertIn("stat -c '%d:%i'", entrypoint)
        self.assertIn("/proc/self/mountinfo", entrypoint)
        self.assertIn("root_mount_options", entrypoint)
        self.assertIn("require_not_mountpoint /go", entrypoint)
        self.assertIn("require_not_mountpoint /root", entrypoint)
        self.assertIn('$5 == "/go" || index($5, "/go/") == 1', entrypoint)
        self.assertIn(
            '$5 == "/root" || index($5, "/root/") == 1', entrypoint
        )
        self.assertIn('assert_go_env GOENV "" fail_go_state', entrypoint)
        self.assertNotIn("eval ", helper)
        self.assertNotIn("go mod download", dockerfile)
        self.assertNotIn("go mod verify", dockerfile)
        self.assertNotIn("go build", dockerfile)
        self.assertNotIn("COPY .", dockerfile)
        for bounded_error in (
            "release producer Go session authority is invalid",
            "release producer Go writable state is invalid",
            "release producer Go supply-chain environment is invalid",
            "release producer Go module authority is invalid",
        ):
            self.assertIn(bounded_error, entrypoint)
        self.assertNotIn("env |", entrypoint)
        self.assertNotIn("go env -json", entrypoint)


if __name__ == "__main__":
    unittest.main()
