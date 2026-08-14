from __future__ import annotations

import configparser
import shlex
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
        self.assertEqual(services["bootstrap"]["command"], ["python", "manage.py", "bootstrap_animemo"])
        for service in ("migration", "bootstrap", "api"):
            self.assertTrue(any("/private:/app/runtime/private" in volume for volume in services[service]["volumes"]))

    def test_build_only_overlay_migrates_trusted_legacy_inputs_without_weakening_production(self):
        production = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        build = (ROOT / "deploy/docker-compose.build.yml").read_text(encoding="utf-8")

        self.assertNotIn("ANIME_JOURNAL_", production)
        self.assertNotIn("FRONTEND_URL", production)
        self.assertIn(
            "ANIMEMO_PUBLIC_ORIGIN: ${ANIMEMO_PUBLIC_ORIGIN:-${FRONTEND_URL:-https://animemo.cc}}",
            build,
        )
        self.assertIn(
            "${ANIMEMO_DATA_ROOT:-${ANIME_JOURNAL_DATA_ROOT:-/data/animemo}}",
            build,
        )
        self.assertIn(
            "${ANIMEMO_DATA_ROOT:-${ANIME_JOURNAL_DATA_ROOT:-/data/animemo}}/postgres:/var/lib/postgresql/data",
            build,
        )
        self.assertIn(
            "${ANIMEMO_DATA_ROOT:-${ANIME_JOURNAL_DATA_ROOT:-/data/animemo}}/redis:/data",
            build,
        )
        self.assertIn(
            "${ANIMEMO_PORT:-${ANIME_JOURNAL_PORT:-8088}}",
            build,
        )
        self.assertLess(build.index("ANIMEMO_PUBLIC_ORIGIN:-"), build.index("FRONTEND_URL:-"))
        self.assertLess(build.index("ANIMEMO_DATA_ROOT:-"), build.index("ANIME_JOURNAL_DATA_ROOT:-"))
        self.assertLess(build.index("ANIMEMO_PORT:-"), build.index("ANIME_JOURNAL_PORT:-"))

    def test_updater_runtime_overlay_injects_only_verified_effective_identity(self):
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

    def test_api_startup_has_no_release_orchestration(self):
        dockerfile = (ROOT / "deploy/backend.Dockerfile").read_text(encoding="utf-8")
        command = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))

        self.assertIn("gunicorn", command)
        self.assertNotIn("migrate", command)
        self.assertNotIn("sync_official_plugins", command)
        self.assertNotIn("bootstrap_animemo", command)
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
        self.assertIn("ReadWritePaths=/var/lib/animemo-updater /data/animemo /run/animemo-updater", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", service)
        self.assertIn("UMask=0077", service)
        self.assertIn("/run/animemo-updater 0750 animemo-updater animemo-api", tmpfiles)
        self.assertIn("socket.AF_UNIX", server)
        self.assertNotIn("socket.AF_INET, socket.SOCK_STREAM", server)

    def test_current_bootstrap_matches_host_agent_docker_identity(self):
        service_source = (ROOT / "deploy/updater/animemo-updater.service").read_text(
            encoding="utf-8"
        )
        service = configparser.ConfigParser(interpolation=None, strict=False)
        service.optionxform = str
        service.read_string(service_source)

        bootstrap = (ROOT / "deploy/bootstrap-updater.sh").read_text(encoding="utf-8")
        logical_lines = bootstrap.replace("\\\n", " ").splitlines()
        runuser_line = next(
            line for line in logical_lines if "runuser" in line and "import-current" in line
        )
        tokens = shlex.split(runuser_line)

        self.assertEqual(tokens[0], "runuser")
        self.assertIn("-u", tokens)
        self.assertIn("-g", tokens)
        self.assertIn("-G", tokens)
        self.assertEqual(tokens[tokens.index("-u") + 1], service["Service"]["User"])
        self.assertEqual(tokens[tokens.index("-g") + 1], service["Service"]["Group"])
        self.assertIn(
            tokens[tokens.index("-G") + 1],
            shlex.split(service["Service"]["SupplementaryGroups"]),
        )
        separator = tokens.index("--")
        self.assertEqual(
            tokens[separator + 1 :],
            ["/usr/local/bin/animemo-updater", "import-current"],
        )

    def test_agent_preflight_and_stable_window_cover_the_public_contract(self):
        deployment = (ROOT / "updater/deployment.py").read_text(encoding="utf-8")

        self.assertIn("MIN_AVAILABLE_MEMORY = 512 * 1024 * 1024", deployment)
        self.assertIn('HEALTH_PATHS = ("/health/", "/", "/login", "/api/schema/", "/api/docs/")', deployment)
        self.assertIn('["postgres", "redis", "api", "web"]', deployment)
        self.assertIn('self._verify_http_paths(("/health/", "/"))', deployment)
        self.assertIn('["/usr/bin/docker", "logs", "--since", since, container]', deployment)
        self.assertIn('logs = f"{result.stdout}\\n{result.stderr}"', deployment)
        self.assertIn("self.verify_recent_backup()", deployment)

    def test_release_source_uses_public_rest_and_local_attestation_bundles(self):
        source = (ROOT / "updater/source.py").read_text(encoding="utf-8")
        requirements = (ROOT / "release/requirements.txt").read_text(encoding="utf-8")

        self.assertIn('GITHUB_API_ROOT = "https://api.github.com"', source)
        self.assertIn(
            'ATTESTATION_BUNDLE_HOST = "tmaproduction.blob.core.windows.net"',
            source,
        )
        self.assertIn("class _RejectRedirects", source)
        self.assertIn('f"/repos/{REPOSITORY}/releases?per_page=100&page={page}"', source)
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
        for credential in (
            '"GH_TOKEN"',
            '"GITHUB_TOKEN"',
            '"GH_CONFIG_DIR"',
            '"DOCKER_CONFIG"',
        ):
            self.assertIn(credential, source)

    def test_agent_switch_is_scoped_and_plugin_compatibility_uses_live_state(self):
        deployment = (ROOT / "updater/deployment.py").read_text(encoding="utf-8")
        executor = (ROOT / "updater/executor.py").read_text(encoding="utf-8")
        command = (
            ROOT / "backend/plugin_host/management/commands/list_enabled_plugin_apis.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"up", "-d", "--no-deps", "--force-recreate"', deployment)
        self.assertIn('"--wait", "--wait-timeout", "120", "api", "web"', deployment)
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

    def test_installer_is_independent_of_the_calling_umask(self):
        installer = (ROOT / "deploy/install-updater.sh").read_text(encoding="utf-8")

        parent_modes = 'chmod 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"'
        release_modes = 'chmod -R a+rX,go-w "$STAGING"'
        service_probe = (
            'runuser -u animemo-updater -g animemo-api -- "$LAUNCHER" version >/dev/null'
        )
        service_start = 'systemctl enable --now "$SERVICE"'

        self.assertIn("python3 runuser systemctl", installer)
        self.assertIn(parent_modes, installer)
        self.assertIn(release_modes, installer)
        self.assertIn(service_probe, installer)
        self.assertLess(installer.index(parent_modes), installer.index('mkdir -p "$STAGING"'))
        self.assertLess(installer.index(release_modes), installer.index('mv "$STAGING" "$RELEASE_ROOT"'))
        self.assertLess(installer.index(service_probe), installer.index(service_start))

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
        fixture_start = gate.index('cat >"$ENV_FILE" <<EOF\n')
        fixture_end = gate.index("\nEOF\n", fixture_start)
        env_fixture = gate[fixture_start:fixture_end]
        production_sources = (
            (ROOT / ".env.production.example").read_text(encoding="utf-8"),
            (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"),
            (ROOT / "deploy/docker-compose.build.yml").read_text(encoding="utf-8"),
            (ROOT / "deploy/frontend.Dockerfile").read_text(encoding="utf-8"),
            (ROOT / "backend/config/settings.py").read_text(encoding="utf-8"),
        )

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
        self.assertIn('BUILD_OVERRIDE_FILE="$CURRENT_ROOT/deploy/docker-compose.build.yml"', gate)
        self.assertIn(
            'local compose_files=(-f "$source_root/deploy/docker-compose.yml" -f "$BUILD_OVERRIDE_FILE")',
            gate,
        )
        self.assertNotIn('if [[ "$source_root" == "$CURRENT_ROOT" ]]', gate)
        base_ready = gate.index(
            'run_compose base_services_start "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" '
            'up -d --wait --wait-timeout 120 postgres redis'
        )
        base_migration = gate.index(
            'run_compose base_migration "$BASE_ROOT" "$JOB_TIMEOUT_SECONDS" '
            'run --rm --no-deps migration'
        )
        base_bootstrap = gate.index('run_compose base_bootstrap "$BASE_ROOT" "$JOB_TIMEOUT_SECONDS"')
        base_api = gate.index(
            'run_compose base_api_start "$BASE_ROOT" "$COMMAND_TIMEOUT_SECONDS" '
            'up -d --no-deps api'
        )
        migration = gate.index(
            'run_compose current_migration "$CURRENT_ROOT" "$JOB_TIMEOUT_SECONDS" '
            'run --rm --no-deps migration'
        )
        bootstrap = gate.index(
            'run_compose current_bootstrap "$CURRENT_ROOT" "$JOB_TIMEOUT_SECONDS" '
            'run --rm --no-deps bootstrap'
        )
        switch = gate.index(
            'run_compose current_api_replace "$CURRENT_ROOT" "$COMMAND_TIMEOUT_SECONDS" '
            'up -d --no-deps --force-recreate api'
        )
        retained = gate.index('PostgreSQL and Redis containers were retained')
        self.assertLess(base_ready, base_migration)
        self.assertLess(base_migration, base_bootstrap)
        self.assertLess(base_bootstrap, base_api)
        self.assertLess(migration, bootstrap)
        self.assertLess(bootstrap, switch)
        self.assertLess(switch, retained)
        self.assertIn('BASE_POSTGRES_ID=', gate)
        self.assertIn('BASE_REDIS_ID=', gate)
        self.assertIn('timeout_command "$HEALTH_TIMEOUT_SECONDS" docker exec -i', gate)
        self.assertIn('local deadline=$((SECONDS + API_WAIT_SECONDS))', gate)
        self.assertIn("--kill-after=\"${TIMEOUT_KILL_AFTER_SECONDS}s\"", gate)
        self.assertIn("diagnostic_api_inspect", gate)
        self.assertNotIn('compose "$source_root" exec -T api python -', gate)

    def test_upgrade_gate_overrides_only_the_legacy_bootstrap_server_command(self):
        production = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
        overlay = (ROOT / "deploy/docker-compose.upgrade-gate.yml").read_text(encoding="utf-8")
        gate = (ROOT / "scripts/stateful-upgrade-gate.sh").read_text(encoding="utf-8")
        bootstrap_overlay = overlay[
            overlay.index("  bootstrap:\n") : overlay.index("  web:\n")
        ]
        base_bootstrap = gate[
            gate.index("run_compose base_bootstrap") : gate.index("run_compose base_api_start")
        ]

        self.assertIn('command: ["python", "manage.py", "bootstrap_animemo"]', production)
        self.assertNotIn("command:", bootstrap_overlay)
        self.assertIn("python manage.py sync_official_plugins", base_bootstrap)
        self.assertIn("exec python manage.py collectstatic --noinput", base_bootstrap)
        self.assertNotIn("gunicorn", base_bootstrap)
        self.assertNotIn("manage.py migrate", base_bootstrap)

        fixture = (ROOT / "scripts/stateful_upgrade_fixture.py").read_text(encoding="utf-8")
        self.assertIn('_migration_applied("site", "0003_installation_state")', fixture)
        self.assertIn('"PRESERVED_INITIALIZED"', fixture)

    def test_first_run_private_host_directory_rejects_links_and_is_not_recursively_chowned(self):
        prepare_host = (ROOT / "deploy/prepare-host.sh").read_text(encoding="utf-8")

        self.assertIn('[ -L "$private_directory" ]', prepare_host)
        self.assertIn('[ ! -d "$private_directory" ]', prepare_host)
        self.assertIn('chown "$APP_UID:$APP_GID" "$private_directory"', prepare_host)
        self.assertNotIn('chown -R "$APP_UID:$APP_GID" "$private_directory"', prepare_host)

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
        self.assertIn("--create-admin was removed", deploy)
        self.assertNotIn("deploy/create-admin.sh", deploy)


if __name__ == "__main__":
    unittest.main()
