import logging
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from animemo_bridge.log_safety import (
    PairingCommandLogFilter,
    install_pairing_log_redactor,
    redact_pairing_command,
)


class LogSafetyTests(unittest.TestCase):
    def test_pairing_argument_is_redacted_without_changing_other_text(self):
        original = "[Production AstrBot] /animemo pair MDCW-NPFT"
        redacted = redact_pairing_command(original)
        self.assertEqual(redacted, "[Production AstrBot] /animemo pair [REDACTED]")
        self.assertNotIn("MDCW-NPFT", redacted)

    def test_filter_rewrites_formatted_log_record(self):
        record = logging.LogRecord(
            "astrbot",
            logging.INFO,
            __file__,
            1,
            "incoming: %s",
            ("/animemo pair ABC-123",),
            None,
        )
        self.assertTrue(PairingCommandLogFilter().filter(record))
        self.assertEqual(record.getMessage(), "incoming: /animemo pair [REDACTED]")
        self.assertEqual(record.args, ())

    def test_install_is_idempotent(self):
        logger = logging.getLogger("animemo-log-safety-test")
        logger.filters.clear()
        install_pairing_log_redactor(logger)
        install_pairing_log_redactor(logger)
        self.assertEqual(
            sum(isinstance(item, PairingCommandLogFilter) for item in logger.filters),
            1,
        )

    def test_non_pairing_messages_are_unchanged(self):
        for text in (
            "/animemo ping",
            "/animemo pair",
            "watch pair ABC-123",
            "a message containing animemo without a pair code",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_pairing_command(text), text)
