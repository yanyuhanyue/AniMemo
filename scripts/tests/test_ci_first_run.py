from __future__ import annotations

import json
import unittest

from scripts import ci_first_run


class _Response:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class _Opener:
    def __init__(self):
        self.requests = []
        self.responses = [
            _Response({"csrf_token": "csrf-value"}),
            _Response({"state": "initialized"}, status=201),
            _Response({"state": "initialized", "accepting_setup": False}),
        ]

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.responses.pop(0)


class FirstRunCiClientTests(unittest.TestCase):
    def test_target_must_be_explicitly_isolated(self):
        ci_first_run.validate_isolated_target(
            "http://127.0.0.1:8088",
            "ci.example.test",
            confirm_isolated=True,
        )

        invalid_targets = (
            ("http://127.0.0.1:8088", "ci.example.test", False),
            ("https://re-anime.cc", "ci.example.test", True),
            ("http://127.0.0.1:8088", "re-anime.cc", True),
            ("http://127.0.0.1:8088/path", "ci.example.test", True),
        )
        for base_url, host, confirmed in invalid_targets:
            with self.subTest(base_url=base_url, host=host, confirmed=confirmed):
                with self.assertRaises(ValueError):
                    ci_first_run.validate_isolated_target(
                        base_url,
                        host,
                        confirm_isolated=confirmed,
                    )

    def test_setup_uses_csrf_then_proves_initialized_state(self):
        opener = _Opener()

        result = ci_first_run.complete_setup(
            opener=opener,
            base_url="http://127.0.0.1:8088",
            host="ci.example.test",
            code="private-one-time-code",
            username="ci-bootstrap-admin",
            email="ci-bootstrap-admin@example.test",
            password="SyntheticStrongPassword!2026",
            timeout=7,
        )

        self.assertEqual(result, {"state": "initialized", "accepting_setup": False})
        self.assertEqual([request.full_url for request, _timeout in opener.requests], [
            "http://127.0.0.1:8088/api/v1/auth/csrf/",
            "http://127.0.0.1:8088/api/v1/setup/",
            "http://127.0.0.1:8088/api/v1/setup/status/",
        ])
        self.assertEqual([timeout for _request, timeout in opener.requests], [7, 7, 7])
        setup_request = opener.requests[1][0]
        self.assertEqual(setup_request.get_method(), "POST")
        self.assertEqual(setup_request.get_header("Host"), "ci.example.test")
        self.assertEqual(setup_request.get_header("X-csrftoken"), "csrf-value")
        self.assertEqual(json.loads(setup_request.data), {
            "code": "private-one-time-code",
            "username": "ci-bootstrap-admin",
            "email": "ci-bootstrap-admin@example.test",
            "password": "SyntheticStrongPassword!2026",
            "password_confirm": "SyntheticStrongPassword!2026",
        })


if __name__ == "__main__":
    unittest.main()
