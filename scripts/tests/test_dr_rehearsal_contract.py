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
        cls.workflow = (ROOT / ".github" / "workflows" / "dr-rehearsal.yml").read_text(encoding="utf-8")
        cls.release_gate = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(encoding="utf-8")

    def test_rehearsal_has_real_dump_then_a_only_destruction_then_fresh_b_restore(self):
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
            "AccessToken.lifetime = timedelta(hours=2)",
            'int(access_token["exp"]) - int(access_token["iat"]) >= 2 * 60 * 60',
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

    def test_cleanup_is_compose_project_scoped_and_non_production(self):
        self.assertIn("docker.compose.project=$PROJECT_A", self.script)
        self.assertNotIn("docker system prune", self.script)
        self.assertNotIn("docker volume prune", self.script)
        self.assertNotIn("ssh ", self.script.lower())
        self.assertNotIn("animemo.cc", self.script)
        self.assertNotIn("re-anime.cc", self.script)
        self.assertNotIn("/data/animemo/postgres", self.script)

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
