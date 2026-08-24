from __future__ import annotations

import inspect
import json
import subprocess
import unittest
from dataclasses import replace

from release.dependency_images import load_dependency_image_authority
from release.registry_transport import (
    BACKOFF_SECONDS,
    COMMAND_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    CommandResult,
    DependencyImageTransportError,
    DiagnosticClassification,
    classify_diagnostic,
    pull_dependency_image,
    sanitize_diagnostic,
)

REAL_QUALIFICATION_502_FIXTURE = b"""redis Pulling
redis Error received unexpected HTTP status: 502 Bad Gateway
postgres Interrupted
Error response from daemon: received unexpected HTTP status: 502 Bad Gateway
Process completed with exit code 1.
"""


def inspect_payload(reference: str, *, os_name: str = "linux", architecture: str = "amd64") -> bytes:
    return json.dumps(
        [{"RepoDigests": [reference], "Os": os_name, "Architecture": architecture}]
    ).encode()


class ScriptedRunner:
    def __init__(self, results: list[CommandResult | BaseException]) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def __call__(self, argv: tuple[str, ...], timeout_seconds: int) -> CommandResult:
        self.calls.append((argv, timeout_seconds))
        if not self.results:
            raise AssertionError(f"unexpected command: {argv}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def missing() -> CommandResult:
    return CommandResult(1, b"", b"Error: No such image")


def failed(text: str) -> CommandResult:
    return CommandResult(1, b"", text.encode())


def pulled() -> CommandResult:
    return CommandResult(0, b"sha256:quiet-output\n", b"")


class RegistryTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = load_dependency_image_authority()
        self.redis = self.authority.image("redis")
        self.postgres = self.authority.image("postgres")

    def pull_after(self, failures: list[CommandResult | BaseException], *, role: str = "redis"):
        image = self.authority.image(role)
        runner = ScriptedRunner(
            [missing(), *failures, pulled(), CommandResult(0, inspect_payload(image.reference), b"")]
        )
        sleeps: list[int] = []
        receipt = pull_dependency_image(
            role,
            run_command=runner,
            sleep=sleeps.append,
            authority=self.authority,
        )
        return receipt, runner, sleeps

    def test_closed_postgres_role_produces_exact_pull_argv(self) -> None:
        receipt, runner, _ = self.pull_after([], role="postgres")
        self.assertEqual(
            runner.calls[1][0],
            (
                "docker",
                "pull",
                "--platform",
                "linux/amd64",
                "--quiet",
                self.postgres.reference,
            ),
        )
        self.assertEqual(receipt.reference, self.postgres.reference)

    def test_closed_redis_role_produces_exact_pull_argv(self) -> None:
        receipt, runner, _ = self.pull_after([])
        self.assertEqual(runner.calls[1][0][-1], self.redis.reference)
        self.assertEqual(receipt.role, "redis")

    def test_unknown_role_fails_before_docker(self) -> None:
        runner = ScriptedRunner([])
        with self.assertRaisesRegex(DependencyImageTransportError, "REFERENCE_INVALID"):
            pull_dependency_image("mysql", run_command=runner, authority=self.authority)
        self.assertEqual(runner.calls, [])

    def test_production_entrypoint_accepts_no_arbitrary_image_argument(self) -> None:
        signature = inspect.signature(pull_dependency_image)
        self.assertNotIn("image", signature.parameters)
        self.assertNotIn("repository", signature.parameters)
        self.assertNotIn("digest", signature.parameters)
        self.assertNotIn("mirror", signature.parameters)

    def test_mutable_reference_cannot_reach_docker(self) -> None:
        invalid_authority = replace(
            self.authority,
            redis=replace(self.redis, repository="docker.io/library/redis:7"),
        )
        runner = ScriptedRunner([])
        with self.assertRaisesRegex(DependencyImageTransportError, "REFERENCE_INVALID"):
            pull_dependency_image(
                "redis", run_command=runner, authority=invalid_authority
            )
        self.assertEqual(runner.calls, [])

    def test_retryable_http_statuses_retry_then_succeed(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                receipt, _, sleeps = self.pull_after(
                    [failed(f"received unexpected HTTP status: {status}")]
                )
                self.assertEqual(receipt.attempts, 2)
                self.assertEqual(sleeps, [BACKOFF_SECONDS[0]])

    def test_retryable_transport_text_retries_then_succeeds(self) -> None:
        diagnostics = (
            "read: connection reset by peer",
            "unexpected EOF",
            "transport EOF",
            "i/o timeout",
            "context deadline exceeded",
            "TLS handshake timeout",
            "temporary failure in name resolution",
            "connection timed out",
        )
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                receipt, _, sleeps = self.pull_after([failed(diagnostic)])
                self.assertEqual(receipt.attempts, 2)
                self.assertEqual(sleeps, [BACKOFF_SECONDS[0]])

    def test_real_failed_qualification_502_fixture_is_retryable(self) -> None:
        self.assertEqual(
            classify_diagnostic(REAL_QUALIFICATION_502_FIXTURE),
            DiagnosticClassification.RETRYABLE,
        )
        receipt, _, _ = self.pull_after(
            [CommandResult(1, b"", REAL_QUALIFICATION_502_FIXTURE)]
        )
        self.assertEqual(receipt.attempts, 2)

    def test_terminal_http_statuses_stop_immediately(self) -> None:
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                runner = ScriptedRunner(
                    [missing(), failed(f"unexpected HTTP status: {status}")]
                )
                sleeps: list[int] = []
                with self.assertRaisesRegex(
                    DependencyImageTransportError, "PULL_TERMINAL"
                ):
                    pull_dependency_image(
                        "redis",
                        run_command=runner,
                        sleep=sleeps.append,
                        authority=self.authority,
                    )
                self.assertEqual(sleeps, [])
                self.assertEqual(len(runner.calls), 2)

    def test_terminal_diagnostics_stop_immediately(self) -> None:
        diagnostics = (
            "manifest unknown",
            "name unknown",
            "repository does not exist",
            "pull access denied",
            "authentication required",
            "no matching manifest for linux/amd64",
            "invalid reference format",
            "digest mismatch",
            "wrong platform",
            "unsupported media type",
            "x509: certificate signed by unknown authority",
            "no space left on device",
            "permission denied while trying to connect to the Docker daemon socket",
            "local filesystem error",
        )
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                runner = ScriptedRunner([missing(), failed(diagnostic)])
                with self.assertRaisesRegex(
                    DependencyImageTransportError, "PULL_TERMINAL"
                ):
                    pull_dependency_image(
                        "redis", run_command=runner, authority=self.authority
                    )
                self.assertEqual(len(runner.calls), 2)

    def test_terminal_signal_wins_over_retryable_signal(self) -> None:
        diagnostic = b"HTTP 502 Bad Gateway followed by HTTP 401 Unauthorized"
        self.assertEqual(
            classify_diagnostic(diagnostic), DiagnosticClassification.TERMINAL
        )

    def test_unknown_failure_is_terminal(self) -> None:
        self.assertEqual(
            classify_diagnostic(b"opaque registry failure"),
            DiagnosticClassification.TERMINAL,
        )

    def test_retry_budget_backoff_and_timeout_are_exact(self) -> None:
        runner = ScriptedRunner(
            [missing(), failed("HTTP 503"), failed("HTTP 503"), failed("HTTP 503")]
        )
        sleeps: list[int] = []
        with self.assertRaisesRegex(
            DependencyImageTransportError, "PULL_TRANSIENT_EXHAUSTED"
        ):
            pull_dependency_image(
                "redis",
                run_command=runner,
                sleep=sleeps.append,
                authority=self.authority,
            )
        pull_calls = [call for call in runner.calls if call[0][1] == "pull"]
        self.assertEqual(len(pull_calls), MAX_ATTEMPTS)
        self.assertEqual(sleeps, list(BACKOFF_SECONDS))
        self.assertTrue(all(timeout == COMMAND_TIMEOUT_SECONDS for _, timeout in pull_calls))

    def test_timeout_retries_and_final_timeout_has_stable_code(self) -> None:
        timeout = subprocess.TimeoutExpired(["docker", "pull"], COMMAND_TIMEOUT_SECONDS)
        runner = ScriptedRunner([missing(), timeout, timeout, timeout])
        sleeps: list[int] = []
        with self.assertRaisesRegex(DependencyImageTransportError, "PULL_TIMEOUT"):
            pull_dependency_image(
                "redis",
                run_command=runner,
                sleep=sleeps.append,
                authority=self.authority,
            )
        self.assertEqual(sleeps, list(BACKOFF_SECONDS))

    def test_verified_cache_hit_skips_network_pull(self) -> None:
        runner = ScriptedRunner(
            [CommandResult(0, inspect_payload(self.redis.reference), b"")]
        )
        receipt = pull_dependency_image(
            "redis", run_command=runner, authority=self.authority
        )
        self.assertEqual(receipt.source, "CACHE_HIT_VERIFIED")
        self.assertEqual(receipt.attempts, 0)
        self.assertEqual(len(runner.calls), 1)

    def test_mutable_tag_only_cache_is_a_miss_then_exact_pull(self) -> None:
        receipt, runner, _ = self.pull_after([])
        self.assertEqual(receipt.source, "NETWORK_PULL_VERIFIED")
        self.assertEqual(runner.calls[0][0][-1], self.redis.reference)

    def test_wrong_digest_cache_is_a_miss_then_exact_pull(self) -> None:
        wrong = self.redis.repository + "@sha256:" + "c" * 64
        runner = ScriptedRunner(
            [
                CommandResult(0, inspect_payload(wrong), b""),
                pulled(),
                CommandResult(0, inspect_payload(self.redis.reference), b""),
            ]
        )
        receipt = pull_dependency_image(
            "redis", run_command=runner, authority=self.authority
        )
        self.assertEqual(receipt.source, "NETWORK_PULL_VERIFIED")
        self.assertEqual(runner.calls[1][0][-1], self.redis.reference)

    def test_wrong_platform_cache_is_a_miss_then_exact_pull(self) -> None:
        runner = ScriptedRunner(
            [
                CommandResult(
                    0,
                    inspect_payload(self.redis.reference, architecture="arm64"),
                    b"",
                ),
                pulled(),
                CommandResult(0, inspect_payload(self.redis.reference), b""),
            ]
        )
        receipt = pull_dependency_image(
            "redis", run_command=runner, authority=self.authority
        )
        self.assertEqual(receipt.source, "NETWORK_PULL_VERIFIED")
        self.assertEqual(runner.calls[1][0][-1], self.redis.reference)

    def test_pull_success_without_exact_repodigest_fails(self) -> None:
        wrong = self.redis.repository + "@sha256:" + "c" * 64
        runner = ScriptedRunner(
            [missing(), pulled(), CommandResult(0, inspect_payload(wrong), b"")]
        )
        with self.assertRaisesRegex(
            DependencyImageTransportError, "LOCAL_DIGEST_MISMATCH"
        ):
            pull_dependency_image("redis", run_command=runner, authority=self.authority)

    def test_pull_success_with_wrong_platform_fails(self) -> None:
        runner = ScriptedRunner(
            [
                missing(),
                pulled(),
                CommandResult(
                    0,
                    inspect_payload(self.redis.reference, os_name="windows"),
                    b"",
                ),
            ]
        )
        with self.assertRaisesRegex(
            DependencyImageTransportError, "PLATFORM_MISMATCH"
        ):
            pull_dependency_image("redis", run_command=runner, authority=self.authority)

    def test_local_docker_daemon_inspect_failure_is_not_a_cache_miss(self) -> None:
        runner = ScriptedRunner([failed("permission denied connecting to Docker daemon")])
        with self.assertRaisesRegex(
            DependencyImageTransportError, "DOCKER_DAEMON_FAILURE"
        ):
            pull_dependency_image("redis", run_command=runner, authority=self.authority)
        self.assertEqual(len(runner.calls), 1)

    def test_diagnostic_is_bounded_and_ansi_removed(self) -> None:
        diagnostic = b"\x1b[31mERROR\x1b[0m " + (b"x" * 10000)
        sanitized = sanitize_diagnostic(diagnostic)
        self.assertLessEqual(len(sanitized), 4096)
        self.assertNotIn("\x1b", sanitized)

    def test_diagnostic_redacts_credentials_and_signed_queries(self) -> None:
        diagnostic = (
            b"Authorization: Bearer top-secret-token\n"
            b"Basic dXNlcjpwYXNz\n"
            b"https://user:password@registry.example/v2/?X-Amz-Signature=abcdef&token=secret"
        )
        sanitized = sanitize_diagnostic(diagnostic)
        for secret in ("top-secret-token", "dXNlcjpwYXNz", "user", "password", "abcdef", "secret"):
            self.assertNotIn(secret, sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_nul_and_non_utf8_diagnostics_fail_closed(self) -> None:
        for diagnostic in (b"error\x00injected", b"\xff"):
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(
                DependencyImageTransportError, "DOCKER_DAEMON_FAILURE"
            ):
                sanitize_diagnostic(diagnostic)

    def test_command_execution_uses_no_shell_or_eval_path(self) -> None:
        source = inspect.getsource(__import__("release.registry_transport", fromlist=["*"]))
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("eval(", source)
        self.assertNotIn("docker image rm", source)
        self.assertNotIn("mirror", source.lower())


if __name__ == "__main__":
    unittest.main()
