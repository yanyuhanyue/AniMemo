from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "wait-for-stable-postgres.sh"
HARNESS = r'''
fake_ready_index=0
pg_isready() {
  local status="${FAKE_READY_SEQUENCE:fake_ready_index:1}"
  printf 'pg_isready:%s\n' "$fake_ready_index" >> "$FAKE_CALL_LOG"
  fake_ready_index=$((fake_ready_index + 1))
  [[ "$*" == *"--timeout 2"* ]] || return 97
  [[ "$status" = "1" ]]
}
psql() {
  [[ "${PGCONNECT_TIMEOUT:-}" = "2" ]] || return 97
  [[ "$*" == *"--host 127.0.0.1 --port 55432"* ]] || return 97
  [[ "$*" == *"--username qualification --dbname postgres --no-password"* ]] || return 97
  printf 'psql\n' >> "$FAKE_CALL_LOG"
  printf '1\n'
}
createdb() {
  [[ "${PGCONNECT_TIMEOUT:-}" = "2" ]] || return 97
  [[ "$*" == *"--host 127.0.0.1 --port 55432"* ]] || return 97
  [[ "$*" == *"--username qualification --no-password"* ]] || return 97
  printf 'createdb:%s\n' "${!#}" >> "$FAKE_CALL_LOG"
  [[ "${FAKE_CREATEDB_FAIL_TARGET:-}" != "${!#}" ]]
}
docker() {
  [[ "$1" = "logs" ]] || return 97
  [[ "$2" = "$QUALIFICATION_POSTGRES_NAME" ]] || return 97
  printf 'docker:logs\n' >> "$FAKE_CALL_LOG"
}
timeout() {
  [[ "${PGCONNECT_TIMEOUT:-}" = "2" ]] || return 97
  [[ "$1" = "--foreground" ]] || return 97
  [[ "$2" = "--signal=TERM" ]] || return 97
  [[ "$3" = "--kill-after=2s" ]] || return 97
  [[ "$4" = "5s" ]] || return 97
  printf 'timeout:%s\n' "$5" >> "$FAKE_CALL_LOG"
  shift 4
  "$@"
}
sleep() {
  [[ "$1" = "2" ]] || return 97
  printf 'sleep:2\n' >> "$FAKE_CALL_LOG"
}
readiness_script="$1"
shift
source "$readiness_script"
'''


def _bash_path() -> str | None:
    if os.name == "nt":
        candidates = (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        )
        return next((str(path) for path in candidates if path.is_file()), None)
    return shutil.which("bash")


class StablePostgresReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bash = _bash_path()
        if self.bash is None:
            self.skipTest("bash is required for the readiness behavior fixture")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log = self.root / "calls.log"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self,
        sequence: str,
        *,
        createdb_fail_target: str = "",
        script: Path = SCRIPT,
    ):
        environment = os.environ.copy()
        environment.update(
            {
                "FAKE_CALL_LOG": self.log.as_posix(),
                "FAKE_READY_SEQUENCE": sequence,
                "FAKE_CREATEDB_FAIL_TARGET": createdb_fail_target,
                "PGPASSWORD": "qualification-only",
                "QUALIFICATION_POSTGRES_NAME": "qualification-postgres",
            }
        )
        return subprocess.run(
            [
                self.bash,
                "-c",
                HARNESS,
                "readiness-fixture",
                script.as_posix(),
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_intermittent_readiness_resets_before_three_stable_queries(self):
        result = self._run("110111")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._calls()
        self.assertEqual(sum(call.startswith("pg_isready:") for call in calls), 6)
        self.assertEqual(calls.count("psql"), 5)
        self.assertEqual(calls.count("timeout:psql"), 5)
        self.assertEqual(calls.count("sleep:2"), 5)
        self.assertEqual(
            [call for call in calls if call.startswith("createdb:")],
            ["createdb:qualification_source", "createdb:qualification_target"],
        )
        self.assertEqual(calls.count("timeout:createdb"), 2)

    def test_sixty_failed_probes_never_create_databases_and_emit_logs(self):
        result = self._run("0" * 60)
        self.assertNotEqual(result.returncode, 0)
        calls = self._calls()
        self.assertEqual(sum(call.startswith("pg_isready:") for call in calls), 60)
        self.assertNotIn("psql", calls)
        self.assertEqual(calls.count("sleep:2"), 60)
        self.assertFalse(any(call.startswith("createdb:") for call in calls))
        self.assertEqual(calls[-1], "docker:logs")

    def test_database_creation_failure_emits_container_logs(self):
        result = self._run(
            "111", createdb_fail_target="qualification_source"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self._calls()[-2:],
            ["createdb:qualification_source", "docker:logs"],
        )

    def test_second_database_creation_failure_emits_container_logs(self):
        result = self._run(
            "111", createdb_fail_target="qualification_target"
        )
        self.assertNotEqual(result.returncode, 0)
        calls = self._calls()
        self.assertEqual(
            [call for call in calls if call.startswith("createdb:")],
            [
                "createdb:qualification_source",
                "createdb:qualification_target",
            ],
        )
        self.assertEqual(calls[-1], "docker:logs")

    def test_command_timeout_contract_mutations_are_caught(self):
        source = SCRIPT.read_text(encoding="utf-8")
        mutations = (
            ("--timeout 2", "--timeout 3"),
            (
                "PGCONNECT_TIMEOUT=2 timeout \\\n      --foreground --signal=TERM --kill-after=2s 5s psql",
                "PGCONNECT_TIMEOUT=3 timeout \\\n      --foreground --signal=TERM --kill-after=2s 5s psql",
            ),
            (
                "PGCONNECT_TIMEOUT=2 timeout \\\n    --foreground --signal=TERM --kill-after=2s 5s createdb",
                "PGCONNECT_TIMEOUT=3 timeout \\\n    --foreground --signal=TERM --kill-after=2s 5s createdb",
            ),
        )
        for index, (before, after) in enumerate(mutations):
            with self.subTest(before=before):
                self.log.unlink(missing_ok=True)
                mutated = self.root / f"mutated-{index}.sh"
                mutated.write_text(
                    source.replace(before, after, 1),
                    encoding="utf-8",
                    newline="\n",
                )
                result = self._run("1" * 60, script=mutated)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._calls()[-1], "docker:logs")


if __name__ == "__main__":
    unittest.main()
