from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from scripts.pull_docker_image import (
    MAX_ATTEMPTS,
    is_retryable_transport_failure,
    pull_image,
)


IMAGE = "docker.io/library/redis@sha256:" + ("a" * 64)


def completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["docker", "pull", "--quiet", IMAGE],
        returncode,
        stdout="",
        stderr=stderr,
    )


class PullDockerImageTests(unittest.TestCase):
    def test_retries_a_502_then_succeeds_with_the_exact_image_argument(self):
        run = mock.Mock(
            side_effect=[
                completed(1, "received unexpected HTTP status: 502 Bad Gateway"),
                completed(0),
            ]
        )
        sleep = mock.Mock()

        self.assertTrue(pull_image(IMAGE, run_command=run, sleep=sleep))

        self.assertEqual(run.call_count, 2)
        run.assert_called_with(
            ["docker", "pull", "--quiet", IMAGE],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        sleep.assert_called_once_with(2)

    def test_stops_immediately_for_a_non_transport_error(self):
        run = mock.Mock(
            return_value=completed(
                1, "pull access denied for private/image, repository does not exist"
            )
        )
        sleep = mock.Mock()

        self.assertFalse(pull_image(IMAGE, run_command=run, sleep=sleep))

        run.assert_called_once()
        sleep.assert_not_called()

    def test_explicit_http_status_is_a_closed_retry_allowlist(self):
        for diagnostic in (
            "HTTP 400 Bad Gateway",
            "HTTP code 500 service unavailable",
            "HTTP status: 404 temporary failure in name resolution",
            "HTTP/1.1 500 service unavailable",
            "status code 400 Bad Gateway",
            "HTTP 502 followed by HTTP 401",
        ):
            with self.subTest(diagnostic=diagnostic):
                self.assertFalse(is_retryable_transport_failure(diagnostic))

        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(
                    is_retryable_transport_failure(f"unexpected HTTP status: {status}")
                )

    def test_retry_budget_is_exactly_three_attempts(self):
        run = mock.Mock(
            return_value=completed(1, "received unexpected HTTP status: 503")
        )
        sleep = mock.Mock()

        self.assertFalse(pull_image(IMAGE, run_command=run, sleep=sleep))

        self.assertEqual(MAX_ATTEMPTS, 3)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(5)])

    def test_command_timeout_is_bounded_and_retryable(self):
        run = mock.Mock(
            side_effect=[
                subprocess.TimeoutExpired(["docker", "pull", IMAGE], 120),
                completed(0),
            ]
        )

        self.assertTrue(pull_image(IMAGE, run_command=run, sleep=mock.Mock()))
        self.assertEqual(run.call_count, 2)

    def test_rejects_control_characters_before_invoking_docker(self):
        run = mock.Mock()

        with self.assertRaises(ValueError):
            pull_image(IMAGE + "\nsecond-command", run_command=run, sleep=mock.Mock())

        run.assert_not_called()

    def test_rejects_a_mutable_tag_before_invoking_docker(self):
        run = mock.Mock()

        with self.assertRaises(ValueError):
            pull_image("docker.io/library/redis:7", run_command=run, sleep=mock.Mock())

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
