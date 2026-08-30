from __future__ import annotations

import ast
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from durability.private_store import AtomicPrivateFile
from installer import production
from installer.production import (
    CandidatePlatformCommandObserver,
    LocalDockerCommandRunner,
    ProductionDoctorAcceptance,
    ProductionFreshInstallPort,
)


class CandidateVmRuntimeRepairTests(unittest.TestCase):
    def test_candidate_fresh_port_materializes_the_exact_private_network_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace = SimpleNamespace(
                app_root=root / "app" / "animemo",
                data_root=root / "data" / "animemo",
                updater_state_root=root / "state" / "animemo",
                updater_runtime_root=root / "runtime" / "animemo",
            )
            fresh = ProductionFreshInstallPort(
                releases=mock.Mock(),
                configuration=mock.Mock(),
                namespace=namespace,
                candidate_network_isolation=True,
            )

            fresh.prepare_roots(mock.Mock())

            override = (
                namespace.data_root
                / "private"
                / "candidate-network-isolation.yml"
            )
            self.assertEqual(
                override.read_bytes(),
                b"networks:\n  animemo:\n    internal: true\n",
            )
            if os.name != "nt":
                self.assertEqual(override.stat().st_mode & 0o777, 0o600)

    def test_regular_fresh_port_does_not_enable_candidate_network_isolation(
        self,
    ) -> None:
        fresh = ProductionFreshInstallPort(
            releases=mock.Mock(),
            configuration=mock.Mock(),
        )
        self.assertFalse(fresh.candidate_network_isolation)

    def test_candidate_command_observers_record_completed_network_boundaries(self) -> None:
        platform_delegate = mock.Mock()
        platform_delegate.run.return_value = SimpleNamespace(returncode=0)
        platform = CandidatePlatformCommandObserver(platform_delegate)
        platform.run(
            ("/usr/bin/apt-get", "update"),
            timeout=30,
            environment={"PATH": "/usr/bin"},
        )
        self.assertEqual(platform.completed_network_commands[0]["returnCode"], 0)
        self.assertEqual(platform.completed_network_commands[0]["operation"], "apt-get")
        platform.run(
            ("/usr/bin/curl", "https://hidden.invalid"),
            timeout=30,
            environment={"PATH": "/usr/bin"},
        )
        self.assertEqual(
            platform.completed_commands[-1]["classification"],
            "UNKNOWN_NETWORK_CAPABILITY",
        )

        docker_delegate = mock.Mock()
        docker_delegate.run.return_value = SimpleNamespace(returncode=0)
        docker = LocalDockerCommandRunner(docker_delegate)
        docker.run(
            ["/usr/bin/docker", "pull", "example.invalid/api@sha256:" + "a" * 64]
        )
        self.assertEqual(docker.completed_external_pulls[0]["returnCode"], 0)
        self.assertEqual(
            docker.completed_external_pulls[0]["referenceDigest"],
            "sha256:" + "a" * 64,
        )
        self.assertEqual(
            docker.completed_commands[0]["externalPullDisposition"],
            "FORBIDDEN_DETECTED",
        )

    def test_compose_up_observer_rejects_implicit_pull_and_accepts_explicit_never(
        self,
    ) -> None:
        delegate = mock.Mock()
        delegate.run.return_value = SimpleNamespace(returncode=0)
        runner = LocalDockerCommandRunner(delegate)

        runner.run(["/usr/bin/docker", "compose", "up", "-d"])
        runner.run(
            ["/usr/bin/docker", "compose", "up", "--pull", "never", "-d"]
        )
        runner.run(["/usr/bin/docker", "compose", "up", "--help"])
        runner.run(["/usr/bin/pg_dump", "--version"])

        self.assertEqual(
            [
                item["externalPullDisposition"]
                for item in runner.completed_commands
            ],
            [
                "FORBIDDEN_DETECTED",
                "EXPLICIT_NEVER",
                "NOT_APPLICABLE",
                "NOT_APPLICABLE",
            ],
        )
        self.assertEqual(runner.completed_commands[-1]["classification"], "LOCAL_ONLY")

    def test_candidate_production_network_bypass_surface_is_closed(self) -> None:
        root = Path(production.__file__).resolve().parents[1]
        sources = {
            path: ast.parse(path.read_text(encoding="utf-8"))
            for path in (
                root / "installer" / "production.py",
                root / "installer" / "platform_bootstrap.py",
                root / "updater" / "deployment.py",
                root / "updater" / "oci.py",
            )
        }
        forbidden_imports: list[str] = []
        for tree in sources.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    forbidden_imports.extend(
                        alias.name
                        for alias in node.names
                        if alias.name in {"requests", "urllib.request"}
                    )
                elif isinstance(node, ast.ImportFrom) and node.module in {
                    "requests",
                    "urllib.request",
                }:
                    forbidden_imports.append(node.module)
        self.assertEqual(forbidden_imports, [])

        production_calls = [
            ast.unparse(node)
            for node in ast.walk(sources[root / "installer" / "production.py"])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "socket"
        ]
        self.assertCountEqual(
            production_calls,
            [
                "socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
                "socket.socket(family, socket.SOCK_STREAM)",
                "socket.create_connection((probe_host, current.listen.port), timeout=5)",
            ],
        )
        deployment_calls = [
            ast.unparse(node)
            for node in ast.walk(sources[root / "updater" / "deployment.py"])
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "http.client.HTTPConnection"
        ]
        self.assertEqual(
            deployment_calls,
            [
                "http.client.HTTPConnection(self._local_probe_host(), port, timeout=5)",
                "http.client.HTTPConnection(self._local_probe_host(), port, timeout=5)",
            ],
        )
        docker_run_argv = [
            [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            ]
            for node in ast.walk(sources[root / "installer" / "production.py"])
            if isinstance(node, ast.List)
            and any(
                isinstance(element, ast.Constant) and element.value == "run"
                for element in node.elts
            )
            and any(
                isinstance(element, ast.Constant)
                and element.value == "/usr/bin/docker"
                for element in node.elts
            )
        ]
        self.assertEqual(len(docker_run_argv), 2)
        for argv in docker_run_argv:
            index = argv.index("run")
            self.assertEqual(argv[index + 1 : index + 3], ["--pull", "never"])

    def test_doctor_acceptance_retains_the_exact_report_for_profile_receipt(self) -> None:
        doctor = ProductionDoctorAcceptance(
            releases=mock.Mock(), compatibility=mock.Mock()
        )
        report = SimpleNamespace(as_dict=lambda: {"overallStatus": "PASS"})
        with mock.patch.object(doctor, "_accept", return_value=report):
            returned = doctor(mock.Mock(), mock.Mock())

        self.assertIs(returned, report)
        self.assertIs(doctor.latest_report, report)

    def test_canonical_acceptance_runs_independent_crud_and_health_observations(
        self,
    ) -> None:
        command_result = SimpleNamespace(
            returncode=0,
            stdout='{"create":true,"delete":true,"read":true,"update":true}\n',
        )
        runner = mock.Mock()
        runner.run.return_value = command_result
        doctor = ProductionDoctorAcceptance(
            releases=mock.Mock(), compatibility=mock.Mock(), runner=runner
        )
        deployment = mock.Mock()
        deployment.probe_api.return_value = None
        deployment.probe_web.return_value = None
        manifest = {"release": {"version": "v1.1.0-rc.19"}}

        observations = doctor._canonical_acceptance(deployment, manifest)

        self.assertEqual(
            [item["name"] for item in observations],
            [
                "application.journal-crud",
                "service.api.health",
                "service.web.health",
            ],
        )
        self.assertTrue(all(item["result"] == "PASS" for item in observations))
        self.assertTrue(
            all(item["receiptDigest"].startswith("sha256:") for item in observations)
        )
        deployment.probe_api.assert_called_once_with(manifest)
        deployment.probe_web.assert_called_once_with(manifest)
        self.assertIn("manage.py", runner.run.call_args.args[0])

    def test_atomic_private_replacement_preserves_existing_posix_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AtomicPrivateFile(root, "authority.json")
            store.write(b"before")
            existing = store.path.lstat()

            with mock.patch(
                "durability.private_store.os.fchown",
                create=True,
            ) as fchown:
                store.write(b"after")

            fchown.assert_called_once()
            self.assertEqual(
                fchown.call_args.args[1:],
                (existing.st_uid, existing.st_gid),
            )

    def test_adopt_updater_waits_for_the_unix_socket_after_service_start(self) -> None:
        fresh = object.__new__(ProductionFreshInstallPort)
        fresh.namespace = SimpleNamespace(
            name="default",
            updater_service="animemo-updater@default.service",
            updater_socket_path=Path("/run/animemo-updater/default/updater.sock"),
        )
        fresh.runner = mock.Mock()
        release = mock.Mock()
        release.version = "v1.1.0-rc.14"
        release.as_dict.return_value = {"version": release.version}
        plan = SimpleNamespace(release=release)
        fresh.releases = mock.Mock()
        fresh.releases.resolve.return_value = release
        fresh.releases.materials_for.return_value.manifest = {"release": {"version": release.version}}
        fresh._ownership_receipt = mock.Mock(return_value=object())
        fresh._locator = mock.Mock(return_value=object())
        fresh._manifest = mock.Mock(return_value={"release": {"version": release.version}})

        receipt_store = mock.Mock()
        receipt_store.path = Path("/tmp/ownership.json")
        fake_pwd = SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=1001))
        fake_grp = SimpleNamespace(getgrnam=lambda _name: SimpleNamespace(gr_gid=1002))
        with mock.patch.object(
            production.os, "name", "posix"
        ), mock.patch.object(
            production.os, "chown", create=True
        ), mock.patch.dict(
            "sys.modules", {"pwd": fake_pwd, "grp": fake_grp}
        ), mock.patch.object(
            production,
            "LocalOwnershipReceiptStore",
            return_value=receipt_store,
        ), mock.patch.object(
            production, "adopt_initial_release"
        ), mock.patch.object(
            fresh, "_wait_for_updater_socket", create=True
        ) as wait_for_socket:
            fresh.adopt_updater(plan)

        wait_for_socket.assert_called_once_with()
        self.assertEqual(
            fresh.runner.run.call_args.args[0],
            [
                "/usr/bin/systemctl",
                "enable",
                "--now",
                "animemo-updater@default.service",
            ],
        )

    def test_updater_socket_wait_is_bounded_and_requires_socket_mode(self) -> None:
        fresh = object.__new__(ProductionFreshInstallPort)
        fresh.namespace = SimpleNamespace(
            updater_socket_path=Path("/run/animemo-updater/default/updater.sock")
        )
        invalid = SimpleNamespace(st_mode=stat.S_IFREG | 0o660)
        valid = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660)
        sleep = mock.Mock()
        with mock.patch.object(
            Path,
            "lstat",
            side_effect=[FileNotFoundError, invalid, valid],
        ), mock.patch.object(
            production,
            "time",
            SimpleNamespace(sleep=sleep),
            create=True,
        ):
            fresh._wait_for_updater_socket()

        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
