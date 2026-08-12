from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class DeploymentUpdaterContractTests(unittest.TestCase):
    def test_production_compose_uses_digest_inputs_and_explicit_jobs(self):
        compose = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]

        self.assertNotIn("build", services["api"])
        self.assertNotIn("build", services["web"])
        self.assertIn("ANIMEMO_API_IMAGE", services["api"]["image"])
        self.assertIn("ANIMEMO_WEB_IMAGE", services["web"]["image"])
        self.assertEqual(services["migration"]["command"], ["python", "manage.py", "migrate", "--noinput"])
        self.assertEqual(services["bootstrap"]["command"], ["python", "manage.py", "sync_official_plugins"])

    def test_api_startup_has_no_release_orchestration(self):
        dockerfile = (ROOT / "deploy/backend.Dockerfile").read_text(encoding="utf-8")
        command = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))

        self.assertIn("gunicorn", command)
        self.assertNotIn("migrate", command)
        self.assertNotIn("sync_official_plugins", command)
        self.assertNotIn("collectstatic", command)

    def test_django_never_mounts_docker_socket(self):
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertIn("/run/animemo-updater", compose)

    def test_host_agent_service_has_fixed_unix_socket_and_honest_hardening(self):
        service = (ROOT / "deploy/updater/animemo-updater.service").read_text(encoding="utf-8")
        tmpfiles = (ROOT / "deploy/updater/animemo-updater.tmpfiles.conf").read_text(encoding="utf-8")
        server = (ROOT / "updater/server.py").read_text(encoding="utf-8")

        self.assertIn("ExecStart=/usr/local/bin/animemo-updater serve", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("PrivateTmp=true", service)
        self.assertIn("ProtectHome=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ReadWritePaths=/var/lib/animemo-updater /data/anime-journal /run/animemo-updater", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("/run/animemo-updater 0750 animemo-updater animemo-api", tmpfiles)
        self.assertIn("socket.AF_UNIX", server)
        self.assertNotIn("socket.AF_INET, socket.SOCK_STREAM", server)

    def test_agent_preflight_and_stable_window_cover_the_public_contract(self):
        deployment = (ROOT / "updater/deployment.py").read_text(encoding="utf-8")

        self.assertIn("MIN_AVAILABLE_MEMORY = 512 * 1024 * 1024", deployment)
        self.assertIn('HEALTH_PATHS = ("/health/", "/", "/login", "/api/schema/", "/api/docs/")', deployment)
        self.assertIn('["postgres", "redis", "api", "web"]', deployment)
        self.assertIn('self._verify_http_paths(("/health/", "/"))', deployment)
        self.assertIn('["/usr/bin/docker", "logs", "--since", since, container]', deployment)
        self.assertIn('logs = f"{result.stdout}\\n{result.stderr}"', deployment)
        self.assertIn("self.verify_recent_backup()", deployment)

    def test_agent_switch_is_scoped_and_plugin_compatibility_uses_live_state(self):
        deployment = (ROOT / "updater/deployment.py").read_text(encoding="utf-8")
        executor = (ROOT / "updater/executor.py").read_text(encoding="utf-8")
        command = (
            ROOT / "backend/plugin_host/management/commands/list_enabled_plugin_apis.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"up", "-d", "--no-deps", "--force-recreate", "api", "web"', deployment)
        self.assertIn('"python", "manage.py", "list_enabled_plugin_apis"', deployment)
        self.assertIn("inspect_enabled_plugin_apis(current)", executor)
        self.assertIn("inspect_enabled_plugin_apis(target_manifest)", executor)
        self.assertIn("refresh=True", executor)
        self.assertIn("verified_manifest != target_manifest", executor)
        self.assertIn("verified_manifest != previous_manifest", executor)
        self.assertIn("PluginDeployment.objects.filter(enabled=True)", command)
        self.assertNotIn("[2]", command)

    def test_agent_private_files_and_socket_are_bound_to_fixed_roots(self):
        commands = (ROOT / "updater/commands.py").read_text(encoding="utf-8")
        deployment = (ROOT / "updater/deployment.py").read_text(encoding="utf-8")
        server = (ROOT / "updater/server.py").read_text(encoding="utf-8")
        state = (ROOT / "updater/state.py").read_text(encoding="utf-8")

        self.assertIn("tempfile.mkstemp", commands)
        self.assertIn("_ensure_private_directory(root, path.parent)", commands)
        self.assertIn("root=self.paths.data_root", deployment)
        self.assertIn("root=self.paths.state_root", deployment)
        self.assertIn("tempfile.mkstemp", state)
        self.assertIn("metadata.st_nlink != 1", state)
        self.assertIn("opened.st_nlink != 1", state)
        self.assertIn("stat.S_ISSOCK(existing.st_mode)", server)
        self.assertNotIn("self.socket_path.unlink(missing_ok=True)", server)

    def test_installer_only_manages_animemo_updater_assets(self):
        installer = (ROOT / "deploy/install-updater.sh").read_text(encoding="utf-8")

        for fixed_target in [
            "/opt/animemo-updater",
            "/var/lib/animemo-updater",
            "/run/animemo-updater",
            "/usr/local/bin/animemo-updater",
            "animemo-updater.service",
        ]:
            self.assertIn(fixed_target, installer)
        for forbidden in [
            "docker compose down",
            "docker stop",
            "systemctl restart docker",
            "systemctl restart nginx",
            "systemctl restart openresty",
            "/opt/1panel/www",
            "cloudflared",
        ]:
            self.assertNotIn(forbidden, installer)

    def test_fresh_release_gate_uses_build_override_and_explicit_jobs(self):
        workflow = (ROOT / ".github/workflows/release-gate.yml").read_text(encoding="utf-8")

        self.assertIn('if [[ -f deploy/docker-compose.build.yml ]]; then', workflow)
        self.assertIn(
            "COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.build.yml",
            workflow,
        )
        ready = workflow.index("up -d --wait --wait-timeout 120 postgres redis")
        migration = workflow.index("run --rm --no-deps migration")
        bootstrap = workflow.index("run --rm --no-deps bootstrap")
        switch = workflow.index("up -d --no-deps api web")
        self.assertLess(ready, migration)
        self.assertLess(migration, bootstrap)
        self.assertLess(bootstrap, switch)

    def test_stateful_gate_migrates_before_scoped_switch_and_retains_data_services(self):
        gate = (ROOT / "scripts/stateful-upgrade-gate.sh").read_text(encoding="utf-8")

        self.assertIn('BUILD_OVERRIDE_FILE="$CURRENT_ROOT/deploy/docker-compose.build.yml"', gate)
        self.assertIn(
            'local compose_files=(-f "$source_root/deploy/docker-compose.yml" -f "$BUILD_OVERRIDE_FILE")',
            gate,
        )
        self.assertNotIn('if [[ "$source_root" == "$CURRENT_ROOT" ]]', gate)
        migration = gate.index('compose "$CURRENT_ROOT" run --rm --no-deps migration')
        bootstrap = gate.index('compose "$CURRENT_ROOT" run --rm --no-deps bootstrap')
        switch = gate.index('compose "$CURRENT_ROOT" up -d --no-deps --force-recreate api')
        retained = gate.index('PostgreSQL and Redis containers were retained')
        self.assertLess(migration, bootstrap)
        self.assertLess(bootstrap, switch)
        self.assertLess(switch, retained)
        self.assertIn('BASE_POSTGRES_ID=', gate)
        self.assertIn('BASE_REDIS_ID=', gate)

    def test_legacy_zip_deployer_is_explicit_bootstrap_or_break_glass_only(self):
        deploy = (ROOT / "deploy/deploy.sh").read_text(encoding="utf-8")

        self.assertIn("--bootstrap", deploy)
        self.assertIn("--break-glass", deploy)
        self.assertIn("normal updates use the AniMemo Update Agent", deploy)
        self.assertIn("deploy/docker-compose.build.yml", deploy)
        ready = deploy.index('stage_compose up -d --wait --wait-timeout 120 postgres redis')
        migration = deploy.index('stage_compose run --rm --no-deps migration')
        bootstrap = deploy.index('stage_compose run --rm --no-deps bootstrap')
        switch = deploy.index('live_compose up -d --no-deps --force-recreate api web')
        self.assertLess(ready, migration)
        self.assertLess(migration, bootstrap)
        self.assertLess(bootstrap, switch)
        self.assertNotIn("stage_compose down", deploy)
        self.assertNotIn("STACK_STOPPED", deploy)


if __name__ == "__main__":
    unittest.main()
