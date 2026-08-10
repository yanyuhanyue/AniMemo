import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from animemo_bridge.time_display import format_status_timestamp


class TimeDisplayTests(unittest.TestCase):
    def test_utc_state_timestamp_is_rendered_as_shanghai_time(self):
        self.assertEqual(
            format_status_timestamp("2026-08-10T05:31:46.255363+00:00"),
            "2026-08-10 13:31:46 (UTC+08:00)",
        )

    def test_zulu_and_naive_values_are_supported(self):
        self.assertEqual(
            format_status_timestamp("2026-08-10T05:31:46Z"),
            "2026-08-10 13:31:46 (UTC+08:00)",
        )
        self.assertEqual(
            format_status_timestamp("2026-08-10T05:31:46"),
            "2026-08-10 13:31:46 (UTC+08:00)",
        )

    def test_missing_or_invalid_values_are_safe(self):
        self.assertEqual(format_status_timestamp(None), "NOT RUN")
        self.assertEqual(format_status_timestamp("not-a-timestamp"), "not-a-timestamp")
