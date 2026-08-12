from __future__ import annotations

import unittest

from updater.redaction import redact


class RedactionTests(unittest.TestCase):
    def test_secrets_are_removed_from_logs(self):
        value = redact(
            "Authorization: Bearer abc.def DB_PASSWORD=hunter2 "
            "https://user:secret@example.test GITHUB_TOKEN=ghp_example refresh_token=very-secret"
        )

        for secret in ["abc.def", "hunter2", "secret@example", "ghp_example", "very-secret"]:
            self.assertNotIn(secret, value)
        self.assertIn("[REDACTED]", value)


if __name__ == "__main__":
    unittest.main()
