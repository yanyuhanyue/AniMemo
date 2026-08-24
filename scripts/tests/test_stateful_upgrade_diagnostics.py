from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "stateful-upgrade-gate.sh"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _git_bash() -> Path | None:
    if os.name == "nt":
        candidates = (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        )
    else:
        candidates = (shutil.which("bash"),)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


class StatefulUpgradeDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = _git_bash()
        if cls.bash is None:
            raise unittest.SkipTest("Git Bash or bash is required for the stateful CLI harness.")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name)
        self.fake_bin = self.temp_root / "bin"
        self.fake_bin.mkdir()
        self.calls = self.temp_root / "docker-calls.log"
        self.state = self.temp_root / "state"
        self.state.mkdir()
        self._write_fake_commands()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_executable(self, name: str, source: str) -> None:
        path = self.fake_bin / name
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def _write_fake_commands(self) -> None:
        self._write_executable(
            "git",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            args="$*"
            if [[ "$args" == *"rev-parse --verify"* ]]; then
              if [[ "$args" == *"HEAD^{commit}"* ]]; then
                printf '%s\n' "$FAKE_HEAD_SHA"
              elif [[ "$args" == *"$FAKE_BASE_SHA^{commit}"* ]]; then
                printf '%s\n' "$FAKE_BASE_SHA"
              else
                printf '%s\n' "$FAKE_HEAD_SHA"
              fi
              exit 0
            fi
            if [[ "$args" == *"merge-base --is-ancestor"* ]]; then
              exit 0
            fi
            if [[ "$args" == *"worktree add --detach"* ]]; then
              target="${@: -2:1}"
              mkdir -p "$target/deploy"
              exit 0
            fi
            exit 0
            """,
        )
        self._write_executable(
            "sudo",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "-n" && "${2:-}" == "true" ]]; then
              exit 0
            fi
            if [[ "${1:-}" == "-n" ]]; then
              shift
            fi
            if [[ "${1:-}" == "install" ]]; then
              # The diagnostics harness never runs Compose. Treat privileged
              # canonical host preparation as a successful fixed-boundary
              # operation without writing Git Bash's system-managed /run.
              exit 0
            fi
            "$@"
            """,
        )
        self._write_executable(
            "python3",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "$*" == *"-m release.registry_transport pull-all --projection compose-env"* ]]; then
              printf '%s\n' \
                'ANIMEMO_POSTGRES_IMAGE=docker.io/library/postgres@sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571' \
                'ANIMEMO_REDIS_IMAGE=docker.io/library/redis@sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf' \
                'DEPENDENCY_IMAGE_AUTHORITY_SHA256=sha256:5731c649d00fcc8bada9ce3fcd92039c09b43bd47c7110d8d335392a51ab37c6'
              exit 0
            fi
            converted=()
            for argument in "$@"; do
              if [[ "$argument" == /* ]] && command -v cygpath >/dev/null 2>&1; then
                converted+=("$(cygpath -w "$argument")")
              else
                converted+=("$argument")
              fi
            done
            exec "$FAKE_REAL_PYTHON" "${converted[@]}"
            """,
        )
        self._write_executable(
            "sleep",
            """
            #!/usr/bin/env bash
            exit 0
            """,
        )
        self._write_executable(
            "docker",
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >>"$FAKE_DOCKER_CALLS"
            args="$*"

            if [[ "$args" == *"config --format json"* ]]; then
              printf '%s\n' '{"name":"animemo-upgrade-test","services":{"postgres":{"image":"docker.io/library/postgres@sha256:075f7ba66bc9b3ce7d6b8b635208ff61cd7cf1a67d71ec530eec5d7ae0cbe571","platform":"linux/amd64","volumes":[{"type":"bind","source":"/tmp/stateful/postgres","target":"/var/lib/postgresql/data"}],"networks":{"upgrade-gate":null}},"redis":{"image":"docker.io/library/redis@sha256:9702d01c1f10c3ea9f48211b4362e44f154ff02d063e6f7268eba804059f53bf","platform":"linux/amd64","volumes":[{"type":"bind","source":"/tmp/stateful/redis","target":"/data"}],"networks":{"upgrade-gate":null}}},"networks":{"upgrade-gate":{"name":"animemo-upgrade-test-network"}}}'
              exit 0
            fi

            matches_once() {
              local kind="$1"
              local pattern="$2"
              [[ -n "$pattern" && "$args" == *"$pattern"* ]] || return 1
              local marker="$FAKE_STATE_DIR/$kind"
              [[ ! -e "$marker" ]] || return 1
              : >"$marker"
            }

            if matches_once ignore_term "${FAKE_DOCKER_IGNORE_TERM_MATCH:-}"; then
              trap '' TERM
              while :; do :; done
            fi
            if [[ -n "${FAKE_DOCKER_BLOCK_ALWAYS_MATCH:-}" && "$args" == *"$FAKE_DOCKER_BLOCK_ALWAYS_MATCH"* ]]; then
              /usr/bin/sleep 10
            fi
            if matches_once block "${FAKE_DOCKER_BLOCK_MATCH:-}"; then
              /usr/bin/sleep 10
            fi
            if matches_once fail "${FAKE_DOCKER_FAIL_MATCH:-}"; then
              exit "${FAKE_DOCKER_FAIL_CODE:-42}"
            fi
            if [[ "${FAKE_DIAGNOSTIC_BLOCK:-false}" == "true" && "$args" == *" logs "* ]]; then
              /usr/bin/sleep 10
            fi
            if [[ "${FAKE_DIAGNOSTIC_IGNORE_TERM:-false}" == "true" && "$args" == *" logs "* ]]; then
              trap '' TERM
              while :; do :; done
            fi

            if [[ "${1:-}" == "exec" ]]; then
              count_file="$FAKE_STATE_DIR/health-count"
              count=0
              [[ ! -f "$count_file" ]] || count="$(cat "$count_file")"
              count=$((count + 1))
              printf '%s' "$count" >"$count_file"
              if ((count <= ${FAKE_HEALTH_FAILURES:-0})); then
                exit 1
              fi
              echo "HTTP /health/: PASS"
              exit 0
            fi

            if [[ "$args" == *" ps -q postgres"* ]]; then
              echo "postgres-stable-id"
            elif [[ "$args" == *" ps -q redis"* ]]; then
              echo "redis-stable-id"
            elif [[ "$args" == "inspect --format"* || "$args" == *" inspect --format"* ]]; then
              inspect_count_file="$FAKE_STATE_DIR/inspect-count"
              inspect_count=0
              [[ ! -f "$inspect_count_file" ]] || inspect_count="$(cat "$inspect_count_file")"
              inspect_count=$((inspect_count + 1))
              printf '%s' "$inspect_count" >"$inspect_count_file"
              if ((inspect_count <= ${FAKE_INSPECT_RESTARTING_COUNT:-0})); then
                echo "restarting true true"
              else
                echo "${FAKE_INSPECT_STATE:-running true false}"
              fi
            fi
            exit 0
            """,
        )

    def run_gate(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "FAKE_BIN": str(self.fake_bin),
                "FAKE_DOCKER_CALLS_WIN": str(self.calls),
                "FAKE_STATE_DIR_WIN": str(self.state),
                "FAKE_BASE_SHA": BASE_SHA,
                "FAKE_HEAD_SHA": HEAD_SHA,
                "FAKE_REAL_PYTHON_WIN": sys.executable,
                "COMPOSE_PROJECT_NAME": "animemo-upgrade-test",
                "RUNNER_TEMP_WIN": str(self.temp_root),
                "STATEFUL_UPGRADE_COMMAND_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_BUILD_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_JOB_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_EXEC_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_HEALTH_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_INSPECT_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_DIAGNOSTIC_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_CLEANUP_TIMEOUT_SECONDS": "1",
                "STATEFUL_UPGRADE_API_WAIT_SECONDS": "5",
                "STATEFUL_UPGRADE_POLL_SECONDS": "0",
                "STATEFUL_UPGRADE_TIMEOUT_KILL_AFTER_SECONDS": "1",
                "STATEFUL_UPGRADE_DIAGNOSTIC_LOG_LINES": "25",
            }
        )
        environment.update(overrides)
        command = (
            'if command -v cygpath >/dev/null 2>&1; then '
            'to_posix() { cygpath -u "$1"; }; '
            'else to_posix() { printf "%s\\n" "$1"; }; fi; '
            'export PATH="$(to_posix "$FAKE_BIN"):$PATH"; '
            'export FAKE_DOCKER_CALLS="$(to_posix "$FAKE_DOCKER_CALLS_WIN")"; '
            'export FAKE_STATE_DIR="$(to_posix "$FAKE_STATE_DIR_WIN")"; '
            'export FAKE_REAL_PYTHON="$(to_posix "$FAKE_REAL_PYTHON_WIN")"; '
            'export RUNNER_TEMP="$(to_posix "$RUNNER_TEMP_WIN")"; '
            'export STATEFUL_UPGRADE_TEMP_ROOT="$RUNNER_TEMP/upgrade-root"; '
            'exec "$(to_posix "$STATEFUL_SCRIPT_WIN")" '
            f'--base {BASE_SHA} --head {HEAD_SHA}'
        )
        environment["STATEFUL_SCRIPT_WIN"] = str(SCRIPT)
        return subprocess.run(
            [str(self.bash), "-lc", command],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

    def output(self, completed: subprocess.CompletedProcess[str]) -> str:
        return completed.stdout + completed.stderr

    def test_blocked_base_api_start_times_out_with_bounded_diagnostics(self) -> None:
        completed = self.run_gate(
            FAKE_DOCKER_BLOCK_MATCH="up -d --no-deps api",
            FAKE_DIAGNOSTIC_BLOCK="true",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 124, output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_start status=start", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_start status=timeout exit_code=124", output)
        self.assertIn("STATEFUL_UPGRADE_COMMAND command=base_api_start status=timeout exit_code=124", output)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(" ps", calls)
        self.assertIn("inspect --format {{json .State}}", calls)
        self.assertIn("logs --no-color --tail 25", calls)
        self.assertNotIn("Stateful production upgrade gate: PASS", output)

    def test_ignore_term_block_is_force_killed_within_bounded_duration(self) -> None:
        started = time.monotonic()
        completed = self.run_gate(FAKE_DOCKER_IGNORE_TERM_MATCH="up -d --no-deps api")
        elapsed = time.monotonic() - started

        output = self.output(completed)
        self.assertIn(completed.returncode, (124, 137), output)
        self.assertLess(elapsed, 8, output)
        self.assertRegex(
            output,
            r"STATEFUL_UPGRADE_PHASE phase=base_api_start status=timeout exit_code=(124|137)",
        )
        self.assertNotIn("Stateful production upgrade gate: PASS", output)

    def test_non_timeout_compose_failure_preserves_original_exit_code(self) -> None:
        completed = self.run_gate(
            FAKE_DOCKER_FAIL_MATCH="run --rm --no-deps migration",
            FAKE_DOCKER_FAIL_CODE="42",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 42, output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_migration status=failed exit_code=42", output)
        self.assertIn("STATEFUL_UPGRADE_COMMAND command=base_migration status=failed exit_code=42", output)
        self.assertNotIn("Stateful production upgrade gate: PASS", output)

    def test_base_bootstrap_overrides_the_legacy_long_running_server_command(self) -> None:
        completed = self.run_gate()

        output = self.output(completed)
        self.assertEqual(completed.returncode, 0, output)
        bootstrap_calls = [
            call
            for call in self.calls.read_text(encoding="utf-8").splitlines()
            if "run --rm --no-deps bootstrap" in call
        ]
        self.assertEqual(len(bootstrap_calls), 2, bootstrap_calls)
        self.assertIn("python manage.py sync_official_plugins", bootstrap_calls[0])
        self.assertIn("exec python manage.py collectstatic --noinput", bootstrap_calls[0])
        self.assertNotIn("gunicorn", bootstrap_calls[0])
        self.assertTrue(bootstrap_calls[1].endswith("run --rm --no-deps bootstrap"), bootstrap_calls[1])

    def test_health_probe_can_fail_then_recover(self) -> None:
        completed = self.run_gate(FAKE_HEALTH_FAILURES="2")

        output = self.output(completed)
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_health status=pass", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=current_api_health status=pass", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=current_restart_health status=pass", output)
        health_count = int((self.state / "health-count").read_text(encoding="utf-8"))
        self.assertGreaterEqual(health_count, 5)
        self.assertIn("Stateful production upgrade gate: PASS", output)
        self.assertIn("STATEFUL_DEPENDENCY_PROJECTION_RECEIPT", output)

    def test_health_probe_hang_times_out_and_never_reaches_seed(self) -> None:
        completed = self.run_gate(
            FAKE_DOCKER_BLOCK_ALWAYS_MATCH="exec -i",
            STATEFUL_UPGRADE_API_WAIT_SECONDS="2",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 124, output)
        self.assertIn("STATEFUL_UPGRADE_COMMAND command=base_api_health_probe status=timeout exit_code=124", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_health status=timeout exit_code=124", output)
        self.assertNotIn("STATEFUL_UPGRADE_PHASE phase=base_state_seed status=start", output)

    def test_forever_unhealthy_api_times_out_fail_closed(self) -> None:
        completed = self.run_gate(
            FAKE_HEALTH_FAILURES="999",
            STATEFUL_UPGRADE_API_WAIT_SECONDS="2",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 124, output)
        self.assertIn("BASELINE release API did not become healthy within 2 seconds", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_health status=timeout exit_code=124", output)
        self.assertNotIn("Stateful production upgrade gate: PASS", output)

    def test_inspect_hang_times_out_fail_closed(self) -> None:
        completed = self.run_gate(
            FAKE_HEALTH_FAILURES="999",
            FAKE_DOCKER_BLOCK_MATCH="inspect --format",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 124, output)
        self.assertIn("STATEFUL_UPGRADE_COMMAND command=base_api_health_inspect status=timeout exit_code=124", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_health status=timeout exit_code=124", output)

    def test_exited_api_fails_immediately_with_diagnostics(self) -> None:
        completed = self.run_gate(
            FAKE_HEALTH_FAILURES="999",
            FAKE_INSPECT_STATE="exited false false",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 1, output)
        self.assertIn("BASELINE release API is not running.", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_health status=failed exit_code=1", output)
        self.assertIn("STATEFUL_UPGRADE_DIAGNOSTICS status=complete", output)

    def test_restarting_api_is_allowed_to_recover(self) -> None:
        completed = self.run_gate(
            FAKE_HEALTH_FAILURES="2",
            FAKE_INSPECT_RESTARTING_COUNT="1",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("STATEFUL_UPGRADE_COMMAND command=base_api_health_inspect status=restarting", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_api_health status=pass", output)
        self.assertIn("Stateful production upgrade gate: PASS", output)

    def test_blocked_diagnostics_are_force_killed_without_overwriting_failure(self) -> None:
        started = time.monotonic()
        completed = self.run_gate(
            FAKE_DOCKER_FAIL_MATCH="run --rm --no-deps migration",
            FAKE_DOCKER_FAIL_CODE="42",
            FAKE_DIAGNOSTIC_IGNORE_TERM="true",
        )
        elapsed = time.monotonic() - started

        output = self.output(completed)
        self.assertEqual(completed.returncode, 42, output)
        self.assertLess(elapsed, 8, output)
        self.assertRegex(
            output,
            r"STATEFUL_UPGRADE_COMMAND command=diagnostic_logs status=timeout exit_code=(124|137)",
        )
        self.assertIn("STATEFUL_UPGRADE_COMMAND command=diagnostic_api_inspect status=pass", output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=base_migration status=failed exit_code=42", output)

    def test_restart_failure_is_fail_closed_and_preserves_exit_code(self) -> None:
        completed = self.run_gate(
            FAKE_DOCKER_FAIL_MATCH="restart api",
            FAKE_DOCKER_FAIL_CODE="33",
        )

        output = self.output(completed)
        self.assertEqual(completed.returncode, 33, output)
        self.assertIn("STATEFUL_UPGRADE_PHASE phase=current_restart status=failed exit_code=33", output)
        self.assertNotIn("STATEFUL_UPGRADE_PHASE phase=current_restart_health status=start", output)
        self.assertNotIn("Stateful production upgrade gate: PASS", output)

    def test_success_path_emits_machine_readable_phase_completion(self) -> None:
        completed = self.run_gate()

        output = self.output(completed)
        self.assertEqual(completed.returncode, 0, output)
        for phase in (
            "base_api_start",
            "base_api_health",
            "base_state_seed",
            "current_api_replace",
            "current_api_health",
            "current_restart",
            "current_restart_health",
            "gate",
        ):
            self.assertIn(f"STATEFUL_UPGRADE_PHASE phase={phase} status=pass", output)
        self.assertIn("Stateful production upgrade gate: PASS", output)


if __name__ == "__main__":
    unittest.main()
