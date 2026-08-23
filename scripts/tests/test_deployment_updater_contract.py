from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class DeploymentUpdaterContractTests(unittest.TestCase):
    def test_production_backup_cli_is_installed_offline_without_rehearsal_helper(self):
        installer = (ROOT / "deploy/install-updater.sh").read_text(encoding="utf-8")
        launcher = (ROOT / "deploy/updater/animemo").read_text(encoding="utf-8")
        production = (ROOT / "durability/backup_production.py").read_text(
            encoding="utf-8"
        )
        cli = (ROOT / "durability/backup_cli.py").read_text(encoding="utf-8")

        self.assertIn("/usr/local/bin/animemo", installer)
        self.assertIn('readlink -- "$ANIMEMO_LAUNCHER"', installer)
        self.assertIn("operator launcher path is foreign", installer)
        self.assertIn('"$ANIMEMO_LAUNCHER" backup --help', installer)
        self.assertIn("-m durability.backup_cli", launcher)
        self.assertIn("from . import backup", production)
        self.assertIn("backup.create_backup", production)
        self.assertIn("backup.verify_backup", production)
        self.assertNotIn("scripts/dr_backup.py", production + cli + launcher)

        release_gate = yaml.safe_load(
            (ROOT / ".github/workflows/release-gate.yml").read_text(encoding="utf-8")
        )
        docker_runs = "\n".join(
            str(step.get("run", "")) for step in release_gate["jobs"]["docker"]["steps"]
        )
        self.assertIn(
            "python -m pip install -r release/requirements.txt", docker_runs
        )
        self.assertIn(
            "python -m pip install -r durability/requirements.txt", docker_runs
        )

    def test_production_compose_uses_digest_inputs_and_explicit_jobs(self):
        compose = yaml.safe_load(
            (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        )
        runtime = yaml.safe_load(
            (ROOT / "updater/docker-compose.runtime.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        runtime_services = runtime["services"]

        self.assertNotIn("build", services["api"])
        self.assertNotIn("build", services["web"])
        self.assertIn("ANIMEMO_API_IMAGE", services["api"]["image"])
        self.assertIn("ANIMEMO_WEB_IMAGE", services["web"]["image"])
        self.assertIn("ANIMEMO_POSTGRES_IMAGE", services["postgres"]["image"])
        self.assertIn("ANIMEMO_REDIS_IMAGE", services["redis"]["image"])
        self.assertEqual(services["web"]["ports"][0]["target"], 80)
        self.assertIn("ANIMEMO_LISTEN_HOST", services["web"]["ports"][0]["host_ip"])
        self.assertIn("ANIMEMO_LISTEN_PORT", services["web"]["ports"][0]["published"])
        self.assertNotIn(".env.production", json.dumps(compose))
        self.assertNotIn("ANIMEMO_DATA_ROOT", json.dumps(compose))
        self.assertIn("ANIMEMO_DATA_ROOT", json.dumps(runtime))
        self.assertNotIn("io.animemo.instance-name", json.dumps(compose))
        self.assertIn("io.animemo.instance-name", json.dumps(runtime))
        self.assertEqual(
            services["migration"]["command"],
            ["python", "manage.py", "migrate", "--noinput"],
        )
        self.assertEqual(
            services["bootstrap"]["command"],
            ["python", "manage.py", "bootstrap_animemo"],
        )
        for service in ("migration", "bootstrap", "api"):
            self.assertTrue(
                any(
                    "/private:/app/runtime/private" in volume
                    for volume in runtime_services[service]["volumes"]
                )
            )

    def test_build_only_overlay_uses_an_explicit_testing_adapter(self):
        production = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        build = (ROOT / "deploy/docker-compose.build.yml").read_text(encoding="utf-8")

        self.assertNotIn("ANIME_JOURNAL_", production)
        self.assertNotIn("FRONTEND_URL", production)
        self.assertNotIn("ANIME_JOURNAL_", build)
        self.assertNotIn("FRONTEND_URL", build)
        self.assertNotIn("ANIMEMO_DATA_ROOT", build)
        self.assertNotIn("ANIMEMO_PORT", build)
        self.assertIn("ANIMEMO_TEST_DATA_ROOT", build)
        self.assertIn("ANIMEMO_TEST_MANAGED_ENV_PATH", build)
        self.assertIn("ANIMEMO_PUBLIC_ORIGIN:?", build)

    def test_updater_runtime_overlay_injects_verified_identity_and_instance_binding(self):
        override = yaml.safe_load(
            (ROOT / "updater/docker-compose.runtime.yml").read_text(encoding="utf-8")
        )
        services = override["services"]

        for key in [
            "ANIMEMO_VERSION",
            "ANIMEMO_COMMIT",
            "ANIMEMO_RELEASE_CHANNEL",
        ]:
            self.assertIn("ANIMEMO_RELEASE_", services["api"]["environment"][key])
        for key in [
            "ANIMEMO_RELEASE_VERSION",
            "ANIMEMO_RELEASE_COMMIT",
            "ANIMEMO_RELEASE_CHANNEL",
        ]:
            self.assertIn(key, services["web"]["environment"])
        rendered = json.dumps(override)
        for key in (
            "ANIMEMO_INSTANCE_NAME",
            "ANIMEMO_INSTANCE_ID",
            "ANIMEMO_COMPOSE_PROJECT",
            "ANIMEMO_MANAGED_ENV_PATH",
            "ANIMEMO_UPDATER_RUNTIME_ROOT",
            "ANIMEMO_DATA_ROOT",
        ):
            self.assertIn(key, rendered)

    def test_api_startup_has_no_release_orchestration(self):
        dockerfile = (ROOT / "deploy/backend.Dockerfile").read_text(encoding="utf-8")
        command = next(
            line for line in dockerfile.splitlines() if line.startswith("CMD ")
        )

        self.assertIn("gunicorn", command)
        self.assertNotIn("migrate", command)
        self.assertNotIn("sync_official_plugins", command)
        self.assertNotIn("bootstrap_animemo", command)
        self.assertNotIn("collectstatic", command)

    def test_django_never_mounts_docker_socket(self):
        compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        runtime = (ROOT / "updater/docker-compose.runtime.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/var/run/docker.sock", compose + runtime)
        self.assertIn("/run/animemo-updater", runtime)

    def test_host_agent_service_has_fixed_unix_socket_and_honest_hardening(self):
        service = (ROOT / "deploy/updater/animemo-updater@.service").read_text(
            encoding="utf-8"
        )
        tmpfiles = (ROOT / "deploy/updater/animemo-updater.tmpfiles.conf").read_text(
            encoding="utf-8"
        )
        server = (ROOT / "updater/server.py").read_text(encoding="utf-8")

        self.assertIn(
            "ExecStart=/usr/local/bin/animemo-updater --instance %i serve",
            service,
        )
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("PrivateTmp=true", service)
        self.assertIn("ProtectHome=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn(
            "ReadWritePaths=/var/lib/animemo-updater/instances/%i /data/animemo-instances/%i /run/animemo-updater/%i",
            service,
        )
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("/run/animemo-updater 0750 animemo-updater animemo-api", tmpfiles)
        self.assertIn("socket.AF_UNIX", server)
        self.assertNotIn("socket.AF_INET, socket.SOCK_STREAM", server)

    def test_initial_adoption_uses_only_the_fixed_private_request(self):
        runtime = (ROOT / "updater/runtime.py").read_text(encoding="utf-8")
        command = (ROOT / "updater/__main__.py").read_text(encoding="utf-8")

        self.assertFalse((ROOT / "deploy/bootstrap-updater.sh").exists())
        self.assertIn(
            'namespace.updater_state_root / "bootstrap/initial-adoption.json"',
            runtime,
        )
        self.assertIn('commands.add_parser("adopt-current"', command)
        self.assertNotIn("RELEASE_MANIFEST_JSON", command)
        self.assertNotIn("import-current", command)

    def test_agent_preflight_and_stable_window_cover_the_public_contract(self):
        deployment = (ROOT / "updater/deployment.py").read_text(encoding="utf-8")

        self.assertIn("MIN_AVAILABLE_MEMORY = 512 * 1024 * 1024", deployment)
        self.assertIn(
            'HEALTH_PATHS = ("/health/", "/", "/login", "/api/schema/", "/api/docs/")',
            deployment,
        )
        self.assertIn('["postgres", "redis", "api", "web"]', deployment)
        self.assertIn('self._verify_http_paths(("/health/", "/"))', deployment)
        self.assertIn(
            '["/usr/bin/docker", "logs", "--since", since, container]', deployment
        )
        self.assertIn('logs = f"{result.stdout}\\n{result.stderr}"', deployment)
        self.assertIn("self.verify_recent_backup()", deployment)

    def test_release_source_uses_public_rest_and_local_attestation_bundles(self):
        source = (ROOT / "updater/source.py").read_text(encoding="utf-8")
        transport = (ROOT / "updater/transport.py").read_text(encoding="utf-8")
        requirements = (ROOT / "release/requirements.txt").read_text(encoding="utf-8")

        self.assertIn('GITHUB_API_ROOT = "https://api.github.com"', source)
        self.assertIn(
            'ATTESTATION_BUNDLE_HOST = "tmaproduction.blob.core.windows.net"',
            source,
        )
        self.assertIn("class _RejectRedirects", source)
        self.assertIn(
            'f"/repos/{REPOSITORY}/releases?per_page=100&page={page}"', source
        )
        self.assertIn('f"/repos/{REPOSITORY}/releases/tags/{version}"', source)
        self.assertIn('f"/repos/{REPOSITORY}/git/ref/tags/{version}"', source)
        self.assertIn('f"/repos/{REPOSITORY}/attestations/{digest}"', source)
        self.assertIn("def get_attestation_bundle", source)
        self.assertIn('path_match.group("repository_id") != str(repository_id)', source)
        self.assertIn("snappy.decompress_raw_len", source)
        self.assertIn("snappy.decompress_raw", source)
        self.assertIn("cramjam==2.11.0", requirements)
        self.assertNotIn('["/usr/bin/gh", "api"', source)
        self.assertNotIn('"--signer-workflow"', source)
        self.assertIn('"auth", "token", "--hostname", "github.com"', source)
        for required_option in (
            '"--bundle"',
            '"--repo"',
            '"--cert-identity"',
            '"--cert-oidc-issuer"',
            '"--source-digest"',
            '"--source-ref"',
            '"--signer-digest"',
            '"--predicate-type"',
        ):
            self.assertIn(required_option, source)
        for isolated_environment_key in (
            '"HOME"',
            '"TMPDIR"',
            '"GH_CONFIG_DIR"',
            '"DOCKER_CONFIG"',
            '"GH_PROMPT_DISABLED"',
        ):
            self.assertIn(isolated_environment_key, source)
        self.assertIn('authenticated["GH_TOKEN"] = token', transport)
        self.assertIn("credential_provider=getattr", source)
        self.assertNotIn('"GITHUB_TOKEN"', source)
        self.assertNotIn('"GITHUB_TOKEN"', transport)
        self.assertNotIn("os.environ.copy()", source)
        self.assertNotIn("os.environ.copy()", transport)

    def test_agent_switch_is_scoped_and_plugin_compatibility_uses_live_state(self):
        deployment = (ROOT / "updater/deployment.py").read_text(encoding="utf-8")
        executor = (ROOT / "updater/executor.py").read_text(encoding="utf-8")
        command = (
            ROOT / "backend/plugin_host/management/commands/list_enabled_plugin_apis.py"
        ).read_text(encoding="utf-8")

        for token in ('"up"', '"-d"', '"--no-deps"', '"--force-recreate"', '"--wait"'):
            self.assertIn(token, deployment)
        self.assertIn('"--wait-timeout"', deployment)
        for token in ('"python"', '"manage.py"', '"list_enabled_plugin_apis"'):
            self.assertIn(token, deployment)
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
            "animemo-updater@.service",
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

    def test_installer_is_independent_of_the_calling_umask(self):
        installer = (ROOT / "deploy/install-updater.sh").read_text(encoding="utf-8")

        parent_modes = 'chmod 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"'
        release_modes = 'chmod -R a+rX,go-w "$STAGING"'
        service_probe = 'runuser -u animemo-updater -g animemo-api -- "$LAUNCHER" version >/dev/null'
        self.assertIn("python3 runuser systemctl", installer)
        self.assertIn(parent_modes, installer)
        self.assertIn(release_modes, installer)
        self.assertIn(service_probe, installer)
        self.assertIn("--no-index", installer)
        self.assertIn('--find-links "$STAGING/wheelhouse"', installer)
        self.assertNotIn('systemctl enable --now "$SERVICE"', installer)
        self.assertLess(
            installer.index(parent_modes), installer.index('mkdir -p "$STAGING"')
        )
        self.assertLess(
            installer.index(release_modes),
            installer.index('mv "$STAGING" "$RELEASE_ROOT"'),
        )

    def test_fresh_release_gate_uses_build_override_and_explicit_jobs(self):
        workflow = (ROOT / ".github/workflows/release-gate.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("test -f deploy/docker-compose.build.yml", workflow)
        self.assertIn("test -f updater/docker-compose.runtime.yml", workflow)
        self.assertIn("python -m pip install -r durability/requirements.txt", workflow)
        self.assertIn(
            'install -d -m 0750 -o "$(id -u)" -g "$(id -g)" /run/animemo-updater',
            workflow,
        )
        self.assertNotIn("if [[ -f deploy/docker-compose.build.yml ]]; then", workflow)
        self.assertIn(
            "COMPOSE_FILE=deploy/docker-compose.yml:updater/docker-compose.runtime.yml:deploy/docker-compose.build.yml",
            workflow,
        )
        ready = workflow.index("up -d --wait --wait-timeout 120 postgres redis")
        migration = workflow.index("run --rm --no-deps migration")
        bootstrap = workflow.index("run --rm --no-deps bootstrap")
        switch = workflow.index("up -d --no-deps api web")
        self.assertLess(ready, migration)
        self.assertLess(migration, bootstrap)
        self.assertLess(bootstrap, switch)

    def test_stateful_gate_migrates_before_scoped_switch_and_retains_data_services(
        self,
    ):
        gate = (ROOT / "scripts/stateful-upgrade-gate.sh").read_text(encoding="utf-8")
        fixture_start = gate.index('cat >"$ENV_FILE" <<EOF\n')
        fixture_end = gate.index("\nEOF\n", fixture_start)
        env_fixture = gate[fixture_start:fixture_end]
        production_sources = (
            (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"),
            (ROOT / "deploy/docker-compose.build.yml").read_text(encoding="utf-8"),
            (ROOT / "deploy/frontend.Dockerfile").read_text(encoding="utf-8"),
            (ROOT / "backend/config/settings.py").read_text(encoding="utf-8"),
        )

        self.assertFalse((ROOT / ".env.production.example").exists())

        self.assertIn("FRONTEND_URL=https://ci.example.test", gate)
        self.assertIn("\nTURNSTILE_ENABLED=false\n", env_fixture)
        self.assertIn(
            "\nVITE_TURNSTILE_SITE_KEY=1x00000000000000000000AA\n",
            env_fixture,
        )
        self.assertIn("test-only compatibility fixtures", gate)
        self.assertIn("SiteSettings database-only", gate)
        self.assertIn("public test", gate)
        for source in production_sources:
            for legacy_input in (
                "VITE_TURNSTILE_SITE_KEY",
                "TURNSTILE_ENABLED",
                "TURNSTILE_SECRET",
                "ANIMEMO_TURNSTILE_SITE_KEY",
            ):
                self.assertNotIn(legacy_input, source)
        self.assertIn(
            'BUILD_OVERRIDE_FILE="$CURRENT_ROOT/deploy/docker-compose.build.yml"', gate
        )
        self.assertIn(
            'local compose_files=(-f "$source_root/deploy/docker-compose.yml" -f "$BUILD_OVERRIDE_FILE")',
            gate,
        )
        self.assertNotIn('if [[ "$source_root" == "$CURRENT_ROOT" ]]', gate)
        base_ready = gate.index(
            'run_compose base_services_start "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" '
            "up -d --wait --wait-timeout 120 postgres redis"
        )
        base_migration = gate.index(
            'run_compose base_migration "$BASE_ROOT" "$JOB_TIMEOUT_SECONDS" '
            "run --rm --no-deps migration"
        )
        base_bootstrap = gate.index(
            'run_compose base_bootstrap "$BASE_ROOT" "$JOB_TIMEOUT_SECONDS"'
        )
        base_api = gate.index(
            'run_compose base_api_start "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" '
            "up -d --no-deps api"
        )
        migration = gate.index(
            'run_compose current_migration "$CURRENT_ROOT" "$JOB_TIMEOUT_SECONDS" '
            "run --rm --no-deps migration"
        )
        bootstrap = gate.index(
            'run_compose current_bootstrap "$CURRENT_ROOT" "$JOB_TIMEOUT_SECONDS" '
            "run --rm --no-deps bootstrap"
        )
        switch = gate.index(
            'run_compose current_api_replace "$CURRENT_ROOT" "$COMMAND_TIMEOUT_SECONDS" '
            "up -d --no-deps --force-recreate api"
        )
        retained = gate.index("PostgreSQL and Redis containers were retained")
        self.assertLess(base_ready, base_migration)
        self.assertLess(base_migration, base_bootstrap)
        self.assertLess(base_bootstrap, base_api)
        self.assertLess(migration, bootstrap)
        self.assertLess(bootstrap, switch)
        self.assertLess(switch, retained)
        self.assertIn("BASE_POSTGRES_ID=", gate)
        self.assertIn("BASE_REDIS_ID=", gate)
        self.assertIn('timeout_command "$HEALTH_TIMEOUT_SECONDS" docker exec -i', gate)
        self.assertIn("local deadline=$((SECONDS + API_WAIT_SECONDS))", gate)
        self.assertIn('--kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s"', gate)
        self.assertIn("diagnostic_api_inspect", gate)
        self.assertNotIn('compose "$source_root" exec -T api python -', gate)

    def test_upgrade_gate_overrides_only_the_legacy_bootstrap_server_command(self):
        production = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        overlay = (ROOT / "deploy/docker-compose.upgrade-gate.yml").read_text(
            encoding="utf-8"
        )
        gate = (ROOT / "scripts/stateful-upgrade-gate.sh").read_text(encoding="utf-8")
        bootstrap_overlay = overlay[
            overlay.index("  bootstrap:\n") : overlay.index("  web:\n")
        ]
        base_bootstrap = gate[
            gate.index("run_compose base_bootstrap") : gate.index(
                "run_compose base_api_start"
            )
        ]

        self.assertIn(
            'command: ["python", "manage.py", "bootstrap_animemo"]', production
        )
        self.assertNotIn("command:", bootstrap_overlay)
        self.assertIn("python manage.py sync_official_plugins", base_bootstrap)
        self.assertIn("exec python manage.py collectstatic --noinput", base_bootstrap)
        self.assertNotIn("gunicorn", base_bootstrap)
        self.assertNotIn("manage.py migrate", base_bootstrap)

        fixture = (ROOT / "scripts/stateful_upgrade_fixture.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('_migration_applied("site", "0003_installation_state")', fixture)
        self.assertIn('"PRESERVED_INITIALIZED"', fixture)

    def test_legacy_host_deployment_entrypoints_are_removed(self):
        for relative in (
            "deploy/prepare-host.sh",
            "deploy/deploy.sh",
            "deploy/smoke-test.sh",
            "deploy/openresty-animemo.conf",
            "deploy/animemo-certbot.cron",
            ".env.production.example",
            "updater/tests/test_prepare_host_permissions.py",
        ):
            with self.subTest(relative=relative):
                self.assertFalse((ROOT / relative).exists())


if __name__ == "__main__":
    unittest.main()
