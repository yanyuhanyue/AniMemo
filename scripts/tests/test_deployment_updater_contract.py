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
