from __future__ import annotations

import copy
import unittest

from updater.public_state import public_operation

CREATED = "2026-08-30T10:00:00Z"
UPDATED = "2026-08-30T10:00:01.123456Z"


def operation() -> dict[str, object]:
    return {
        "id": "a" * 32,
        "kind": "apply_update",
        "status": "succeeded",
        "createdAt": CREATED,
        "updatedAt": UPDATED,
        "metadata": {"private": "C:\\private\\release.json"},
        "events": [
            {
                "status": "idle",
                "at": CREATED,
                "detail": "private creation detail",
            },
            {
                "status": "preflight",
                "at": "2026-08-30T10:00:00.100000Z",
                "detail": "private preflight detail",
            },
            {
                "status": "fetching",
                "at": "2026-08-30T10:00:00.200000Z",
                "detail": "private fetch detail",
            },
            {
                "status": "verifying",
                "at": "2026-08-30T10:00:00.300000Z",
                "detail": "private verification detail",
            },
            {
                "status": "pulling",
                "at": "2026-08-30T10:00:00.400000Z",
                "detail": "private acquisition detail",
            },
            {
                "status": "switching",
                "at": "2026-08-30T10:00:00.500000Z",
                "detail": "private switch detail",
            },
            {
                "status": "verifying_health",
                "at": "2026-08-30T10:00:00.600000Z",
                "detail": "private health detail",
            },
            {
                "status": "succeeded",
                "at": UPDATED,
                "detail": "private success detail",
            },
        ],
    }


class PublicOperationProjectionTests(unittest.TestCase):
    def assert_invalid(self, value: object) -> None:
        projected = public_operation(value)
        self.assertEqual(projected["status"], "invalid_operation_state")
        self.assertEqual(projected["createdAt"], "")
        self.assertEqual(projected["updatedAt"], "")
        self.assertEqual(
            projected["events"],
            [
                {
                    "status": "invalid_operation_state",
                    "at": "",
                    "detail": "Operation state is unavailable",
                }
            ],
        )

    def test_valid_operation_is_exactly_projected_without_persisted_detail(self):
        projected = public_operation(operation())

        self.assertEqual(
            set(projected),
            {"id", "kind", "status", "createdAt", "updatedAt", "events"},
        )
        self.assertEqual(projected["status"], "succeeded")
        self.assertEqual(projected["createdAt"], CREATED)
        self.assertEqual(projected["updatedAt"], UPDATED)
        self.assertNotIn("metadata", projected)
        self.assertNotIn("private", repr(projected).lower())
        self.assertTrue(
            all(set(event) == {"status", "at", "detail"} for event in projected["events"])
        )

    def test_timestamp_requires_semantic_canonical_utc_round_trip(self):
        invalid_timestamps = (
            "2026-99-30T10:00:00Z",
            "2026-02-31T10:00:00Z",
            "2026-08-30T25:00:00Z",
            "2026-08-30T10:00:00.1Z",
            "2026-08-30T10:00:00+00:00",
        )
        for timestamp in invalid_timestamps:
            with self.subTest(timestamp=timestamp):
                candidate = operation()
                candidate["updatedAt"] = timestamp
                candidate["events"][-1]["at"] = timestamp
                self.assert_invalid(candidate)

    def test_status_event_and_timeline_mismatches_invalidate_whole_operation(self):
        candidates = []

        status_mismatch = operation()
        status_mismatch["status"] = "manual_recovery_required"
        candidates.append(status_mismatch)

        transition_mismatch = operation()
        transition_mismatch["events"][1]["status"] = "succeeded"
        candidates.append(transition_mismatch)

        created_mismatch = operation()
        created_mismatch["createdAt"] = "2026-08-30T09:59:59Z"
        candidates.append(created_mismatch)

        updated_mismatch = operation()
        updated_mismatch["updatedAt"] = "2026-08-30T10:00:02Z"
        candidates.append(updated_mismatch)

        reversed_time = operation()
        reversed_time["events"][2]["at"] = "2026-08-30T09:59:59Z"
        candidates.append(reversed_time)

        malformed_event = operation()
        malformed_event["events"][2] = {"status": ["fetching"], "at": UPDATED}
        candidates.append(malformed_event)

        for index, candidate in enumerate(candidates):
            with self.subTest(index=index):
                self.assert_invalid(copy.deepcopy(candidate))


if __name__ == "__main__":
    unittest.main()
