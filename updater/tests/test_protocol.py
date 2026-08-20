from __future__ import annotations

import unittest

from updater.errors import RequestRejected
from updater.protocol import validate_request


class ProtocolAllowlistTests(unittest.TestCase):
    def test_documented_operations_are_accepted(self):
        examples = [
            {"operation": "get_status", "params": {}},
            {"operation": "list_releases", "params": {"channel": "stable", "refresh": True}},
            {"operation": "check_update", "params": {"channel": "rc"}},
            {"operation": "plan_update", "params": {"version": "v1.2.0-rc.3"}},
            {"operation": "apply_update", "params": {"planId": "a" * 32, "confirmation": "APPLY v1.2.0-rc.3"}},
            {"operation": "rollback_previous", "params": {"confirmation": "ROLLBACK PREVIOUS"}},
            {
                "operation": "rollback_previous",
                "params": {
                    "confirmation": "ROLLBACK PREVIOUS",
                    "source": "local-bundle",
                    "bundlePayload": "/media/previous-portable.tar",
                    "releaseAttestation": "/media/previous-release-attestation.json",
                },
            },
            {"operation": "get_operation", "params": {"operationId": "b" * 32}},
            {"operation": "get_logs", "params": {"operationId": "b" * 32, "limit": 100}},
        ]

        for request in examples:
            with self.subTest(request=request):
                self.assertEqual(validate_request(request), request)

    def test_arbitrary_control_surfaces_are_rejected(self):
        attacks = [
            {"operation": "run_command", "params": {"command": "rm -rf /"}},
            {
                "operation": "reconcile",
                "params": {"operationId": "a" * 32, "confirmation": "RECONCILE " + "a" * 32},
            },
            {"operation": "apply_update", "params": {"planId": "a" * 32, "confirmation": "APPLY v1.0.0", "service": "postgres"}},
            {"operation": "plan_update", "params": {"version": "v1.0.0", "compose_path": "/other/project"}},
            {"operation": "plan_update", "params": {"version": "v1.0.0", "image": "evil/repo:latest"}},
            {"operation": "list_releases", "params": {"channel": "stable", "url": "https://evil.example"}},
            {"operation": "get_status", "params": {"path": "../../etc/shadow"}},
            {"operation": "get_status", "params": {}, "command": "docker ps"},
        ]

        for request in attacks:
            with self.subTest(request=request), self.assertRaises(RequestRejected):
                validate_request(request)

    def test_version_and_identifier_injection_are_rejected(self):
        for request in [
            {"operation": "plan_update", "params": {"version": "v1.0.0; rm -rf /"}},
            {"operation": "apply_update", "params": {"planId": "../escape", "confirmation": "APPLY v1.0.0"}},
            {"operation": "get_operation", "params": {"operationId": "../escape"}},
        ]:
            with self.subTest(request=request), self.assertRaises(RequestRejected):
                validate_request(request)

    def test_plan_update_source_is_closed_and_local_bundle_requires_explicit_pair(self):
        accepted = (
            {"operation": "plan_update", "params": {"version": "v1.0.0"}},
            {
                "operation": "plan_update",
                "params": {"version": "v1.0.0", "source": "github"},
            },
            {
                "operation": "plan_update",
                "params": {
                    "version": "v1.0.0",
                    "source": "official-mirror",
                },
            },
        )
        for request in accepted:
            with self.subTest(request=request):
                self.assertEqual(validate_request(request), request)

        local = {
            "operation": "plan_update",
            "params": {
                "version": "v1.0.0",
                "source": "local-bundle",
                "bundlePayload": "/media/animemo-v1.0.0-portable.tar",
                "releaseAttestation": "/media/animemo-v1.0.0-release-attestation.json",
            },
        }
        self.assertEqual(validate_request(local), local)

        for params in (
            {"version": "v1.0.0", "source": "local-bundle"},
            {
                "version": "v1.0.0",
                "source": "local-bundle",
                "bundlePayload": "relative.tar",
                "releaseAttestation": "/media/proof.json",
            },
            {
                "version": "v1.0.0",
                "source": "github",
                "bundlePayload": "/media/payload.tar",
                "releaseAttestation": "/media/proof.json",
            },
        ):
            with self.subTest(params=params), self.assertRaises(RequestRejected):
                validate_request({"operation": "plan_update", "params": params})

        for source in ("auto", "geo", "https://attacker.invalid/release"):
            with self.subTest(source=source), self.assertRaises(RequestRejected):
                validate_request(
                    {
                        "operation": "plan_update",
                        "params": {"version": "v1.0.0", "source": source},
                    }
                )


if __name__ == "__main__":
    unittest.main()
