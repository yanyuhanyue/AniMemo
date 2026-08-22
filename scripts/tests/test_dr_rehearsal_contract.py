from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class DisasterRecoveryRehearsalContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "scripts" / "dr-rehearsal.sh").read_text(encoding="utf-8")
        cls.path_guard = (ROOT / "scripts" / "dr_recovery_paths.py").read_text(encoding="utf-8")
        cls.fixture = (ROOT / "scripts" / "stateful_upgrade_fixture.py").read_text(encoding="utf-8")
        cls.upgrade_gate = (ROOT / "scripts" / "stateful-upgrade-gate.sh").read_text(encoding="utf-8")
        cls.workflow = (ROOT / ".github" / "workflows" / "dr-rehearsal.yml").read_text(encoding="utf-8")
        cls.release_gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")

    def test_rehearsal_has_real_dump_then_a_only_destruction_then_fresh_b_restore(self):
        for required in (
            "ANIMEMO_CONFIG_REVISION=",
            "ANIMEMO_TEST_DATA_ROOT=",
            "ANIMEMO_LISTEN_HOST=127.0.0.1",
            "ANIMEMO_LISTEN_PORT=8088",
            "ANIMEMO_POSTGRES_IMAGE=docker.io/library/postgres@sha256:",
            "ANIMEMO_REDIS_IMAGE=docker.io/library/redis@sha256:",
        ):
            self.assertIn(required, self.script)
        dump = self.script.index("pg_dump --format=plain --no-owner --no-privileges")
        backup = self.script.index('dr_backup.py" create')
        destroy_a = self.script.index("== Destroy isolated instance A only ==")
        restore = self.script.index('dr_backup.py" restore')
        fresh_database = self.script.index("SELECT count(*) FROM pg_tables")
        postgres_restore = self.script.index('gzip -dc "$DATA_B/database.sql.gz"')
        self.assertLess(dump, backup)
        self.assertLess(backup, destroy_a)
        self.assertLess(destroy_a, restore)
        self.assertLess(restore, fresh_database)
        self.assertLess(fresh_database, postgres_restore)
        self.assertIn('PROJECT_A="${PROJECT_PREFIX}-a"', self.script)
        self.assertIn('PROJECT_B="${PROJECT_PREFIX}-b"', self.script)
        self.assertIn('test ! -e "$DATA_B"', self.script)
        self.assertIn('test "$(compose b exec -T redis redis-cli --raw DBSIZE', self.script)

    def test_restored_private_bytes_are_verified_as_root_before_runtime_ownership(self):
        backup_verify = self.script.index('dr_backup.py" verify')
        restore = self.script.index('dr_backup.py" restore')
        privileged_marker = (
            'test "$(as_root cat "$DATA_B/private/dr-private.txt")" = '
            '"dr-private-state-v1"'
        )
        self.assertTrue(
            privileged_marker in self.script,
            "privileged restored-private byte validation is absent",
        )
        privileged_validation = self.script.index(privileged_marker)
        runtime_chown = self.script.index(
            'chown -R 10001:10001 "$DATA_B/private"'
        )
        final_mode = self.script.index('chmod 0700 "$DATA_B/private"')

        self.assertLess(backup_verify, restore)
        self.assertLess(restore, privileged_validation)
        self.assertLess(privileged_validation, runtime_chown)
        self.assertLess(runtime_chown, final_mode)
        self.assertNotIn(
            'test "$(cat "$DATA_B/private/dr-private.txt")"',
            self.script,
        )
        self.assertIn('as_root chmod 0700 "$DATA_B/private"', self.script)
        self.assertIn(
            'as_root chown -R 10001:10001 "$DATA_B/private"', self.script
        )
        for line in self.script.splitlines():
            if "$DATA_B/private" in line and "chmod" in line:
                with self.subTest(permission_line=line):
                    self.assertNotRegex(line, r"\b(?:0644|0755|0777)\b|a\+r")

    def test_rehearsal_covers_authoritative_state_and_restore_security_actions(self):
        required = (
            "stateful_upgrade_fixture.py seed",
            "stateful_upgrade_fixture.py verify",
            'ReleaseSlots(root / "releases")',
            "slots.promote(second",
            "OperationStore(root)",
            "post-restore durable write probe",
            'UpdateLock(root / "update.lock")',
            'chown -R "$UPDATER_FIXTURE_UID:$UPDATER_GID"',
            "MediaStorageBackend.objects.update_or_create",
            "StoragePoolService.create_media",
            "StoragePoolService.open_reference",
            "site_settings.site_avatar.open",
            "external R2 inventory is not exercised",
            "dr-private.txt",
            "SiteSettings.load()",
            "update_provider_configuration",
            "rotate_authentication_epoch --confirm-restore",
            "old-access-token",
            "old-refresh-token",
            "csrf_payload = csrf_response.json()",
            "old_refresh_csrf_payload = old_refresh_csrf.json()",
            "login_payload = login_response.json()",
            "refresh_payload = refresh_response.json()",
            'HTTP_ORIGIN="https://dr.example.test"',
            'HTTP_REFERER="https://dr.example.test/"',
            "DR_ACCESS_TOKEN_MIN_REMAINING_SECONDS = 2 * 60 * 60",
            "DR_ACCESS_TOKEN_ISSUANCE_MARGIN_SECONDS = 5 * 60",
            "original_access_token_lifetime = AccessToken.lifetime",
            "AccessToken.lifetime = original_access_token_lifetime",
            "remaining_seconds >= DR_ACCESS_TOKEN_MIN_REMAINING_SECONDS",
            'old_access_token = AccessToken(old_access)',
            'int(old_access_token["exp"]) > int(time()) + 300',
            "old access token expired before epoch rejection proof",
            '"/api/v1/token/"',
            '"/api/v1/token/refresh/"',
            "old_access_response.status_code == 401",
            "old_refresh_response.status_code == 401",
            "login_response.status_code == 200",
            "refresh_response.status_code == 200",
            '"/api/v1/setup/status/"',
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.script)
        for marker in ("IntegrationConnection", "PluginPackageBlob", "InstallationState.load()"):
            with self.subTest(fixture_marker=marker):
                self.assertIn(marker, self.fixture)
        self.assertLess(
            self.script.index("rotate_authentication_epoch --confirm-restore"),
            self.script.index("DR media graph and restored authentication/refresh verification: PASS"),
        )
        self.assertNotIn("issue_token_pair", self.script)
        self.assertNotIn("response.data", self.script)
        self.assertNotIn(
            'int(access_token["exp"]) - int(access_token["iat"]) >= 2 * 60 * 60',
            self.script,
        )

    def test_access_token_fixture_restores_lifetime_and_asserts_remaining_validity(self):
        original_lifetime = self.script.index(
            "original_access_token_lifetime = AccessToken.lifetime"
        )
        override_lifetime = self.script.index(
            "AccessToken.lifetime = timedelta(", original_lifetime
        )
        login = self.script.index('"/api/v1/token/"', override_lifetime)
        restore_lifetime = self.script.index(
            "AccessToken.lifetime = original_access_token_lifetime", login
        )
        remaining_validity = self.script.index("remaining_seconds = (", restore_lifetime)
        remaining_assertion = self.script.index(
            "remaining_seconds >= DR_ACCESS_TOKEN_MIN_REMAINING_SECONDS",
            remaining_validity,
        )

        self.assertLess(original_lifetime, override_lifetime)
        self.assertLess(override_lifetime, login)
        self.assertLess(login, restore_lifetime)
        self.assertLess(restore_lifetime, remaining_validity)
        self.assertLess(remaining_validity, remaining_assertion)
        self.assertRegex(
            self.script,
            r"seconds=\(\s*DR_ACCESS_TOKEN_MIN_REMAINING_SECONDS\s*"
            r"\+\s*DR_ACCESS_TOKEN_ISSUANCE_MARGIN_SECONDS\s*\)",
        )
        self.assertRegex(
            self.script,
            r"remaining_seconds\s*=\s*\(\s*"
            r'int\(access_token\["exp"\]\)\s*-\s*'
            r"int\(timezone\.now\(\)\.timestamp\(\)\)\s*\)",
        )
        self.assertIn(
            "finally:\n    AccessToken.lifetime = original_access_token_lifetime",
            self.script,
        )

    def test_destructive_paths_are_canonical_direct_children_and_revalidated(self):
        for marker in (
            "canonical-directory",
            '--path "${RUNNER_TEMP:-${TMPDIR:-/tmp}}"',
            "prepare-temp-root",
            "validate-delete",
            'remove_instance_root "$DATA_A"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.script)
        self.assertIn('if ".." in path.parts:', self.path_guard)
        self.assertIn("candidate.parent != parent", self.path_guard)
        self.assertIn("target.parent != parent", self.path_guard)
        self.assertIn("_is_link_or_reparse", self.path_guard)
        self.assertNotIn('as_root rm -rf -- "$DATA_A"', self.script)
        for line in self.script.splitlines():
            if "rm -rf --" in line:
                self.assertRegex(line, r'"\$safe_(?:root|target)"')

    def test_embedded_python_blocks_compile(self):
        blocks = []
        current = None
        for line in self.script.splitlines():
            if current is None and line.endswith("<<'PY'"):
                current = []
                continue
            if current is not None and line == "PY":
                blocks.append("\n".join(current) + "\n")
                current = None
                continue
            if current is not None:
                current.append(line)
        self.assertIsNone(current, "unterminated embedded Python block")
        self.assertGreaterEqual(len(blocks), 6)
        for index, block in enumerate(blocks):
            with self.subTest(block=index):
                compile(block, f"dr-rehearsal-inline-{index}.py", "exec")

    def test_runtime_release_fixture_binds_installer_materials_identity(self):
        self.assertIn(
            'installer_materials_sha256="sha256:" + "f" * 64',
            self.script,
        )

    def test_cleanup_is_compose_project_scoped_and_non_production(self):
        self.assertIn("docker.compose.project=$PROJECT_A", self.script)
        self.assertNotIn("docker system prune", self.script)
        self.assertNotIn("docker volume prune", self.script)
        self.assertNotIn("ssh ", self.script.lower())
        self.assertNotIn("animemo.cc", self.script)
        self.assertNotIn("re-anime.cc", self.script)
        self.assertNotIn("/data/animemo/postgres", self.script)

    def test_stateful_fixture_callers_use_only_fixed_container_metadata_paths(self):
        dr_calls = [
            line.strip()
            for line in self.script.splitlines()
            if "stateful_upgrade_fixture.py" in line
        ]
        upgrade_calls = [
            line.strip()
            for line in self.upgrade_gate.splitlines()
            if "stateful_upgrade_fixture.py" in line
        ]

        self.assertEqual(len(dr_calls), 3)
        self.assertEqual(len(upgrade_calls), 4)
        for line in dr_calls:
            self.assertRegex(
                line,
                r"stateful_upgrade_fixture\.py (?:seed --output|verify --input) "
                r"/app/ci-meta/stateful\.json$",
            )
        for line in upgrade_calls:
            self.assertRegex(
                line,
                r"stateful_upgrade_fixture\.py (?:seed --output|verify --input) "
                r"/app/ci-meta/base-state\.json$",
            )

    def test_workflow_is_manual_reusable_exact_sha_and_isolated(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("workflow_call:", self.workflow)
        self.assertIn("ref: ${{ inputs.candidate_sha }}", self.workflow)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"', self.workflow)
        self.assertIn("scripts.tests.test_dr_backup", self.workflow)
        self.assertIn("scripts.tests.test_dr_recovery_paths", self.workflow)
        self.assertIn("scripts.tests.test_dr_recovery_paths", self.release_gate)
        self.assertIn("bash scripts/dr-rehearsal.sh --candidate-sha", self.workflow)
        for workflow in (self.workflow, self.release_gate):
            self.assertIn(
                "DR_REHEARSAL_TEMP_ROOT: ${{ runner.temp }}/animemo-dr-",
                workflow,
            )
            job_env_prefix = workflow.split("steps:", 1)[0]
            self.assertNotIn("runner.temp", job_env_prefix)
        self.assertNotIn("animemo.cc", self.workflow)
        self.assertNotIn("re-anime.cc", self.workflow)
        self.assertNotIn("BANGUMI_OAUTH_CLIENT_SECRET", self.workflow)


if __name__ == "__main__":
    unittest.main()
