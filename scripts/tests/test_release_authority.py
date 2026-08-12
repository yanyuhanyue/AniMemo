from __future__ import annotations

import unittest

from scripts.release_authority import ReleaseAuthorityError, validate_release_authority


def needs(*, preflight="success", full_ci="success", release_gate="success", performance="skipped"):
    return {
        "preflight": {"result": preflight},
        "full-ci": {"result": full_ci},
        "full-release-gate": {"result": release_gate},
        "performance": {"result": performance},
    }


class ReleaseAuthorityTests(unittest.TestCase):
    def test_beta_accepts_only_an_intentionally_skipped_performance_gate(self):
        self.assertEqual(validate_release_authority("beta", needs()), {"channel": "beta", "status": "PASS"})
        for result in ("success", "failure", "cancelled"):
            with self.subTest(result=result), self.assertRaises(ReleaseAuthorityError):
                validate_release_authority("beta", needs(performance=result))

    def test_rc_requires_the_performance_gate_to_succeed(self):
        self.assertEqual(
            validate_release_authority("rc", needs(performance="success")),
            {"channel": "rc", "status": "PASS"},
        )
        for result in ("skipped", "failure", "cancelled"):
            with self.subTest(result=result), self.assertRaises(ReleaseAuthorityError):
                validate_release_authority("rc", needs(performance=result))

    def test_existing_release_gates_always_fail_closed(self):
        cases = (
            needs(preflight="failure"),
            needs(full_ci="failure"),
            needs(release_gate="cancelled"),
        )
        for state in cases:
            with self.subTest(state=state), self.assertRaises(ReleaseAuthorityError):
                validate_release_authority("beta", state)

    def test_unknown_channel_or_missing_result_is_rejected(self):
        with self.assertRaises(ReleaseAuthorityError):
            validate_release_authority("stable", needs())
        malformed = needs()
        del malformed["full-ci"]
        with self.assertRaises(ReleaseAuthorityError):
            validate_release_authority("beta", malformed)


if __name__ == "__main__":
    unittest.main()
