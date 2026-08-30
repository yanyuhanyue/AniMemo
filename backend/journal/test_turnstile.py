from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.test import SimpleTestCase

from .turnstile import verify_turnstile


class TurnstileFailureLoggingTests(SimpleTestCase):
    @patch("journal.turnstile.logger.warning")
    @patch("journal.turnstile.requests.post")
    @patch("journal.turnstile.resolve_turnstile_config")
    def test_failure_log_contains_only_stable_stage_and_exception_class(
        self,
        resolve_config,
        post,
        warning,
    ):
        sentinels = {
            "secret": "TURNSTILE_SECRET_CANARY",
            "token": "TURNSTILE_TOKEN_CANARY",
            "url": "https://private.invalid/signed?secret=URL_CANARY",
            "remote_ip": "203.0.113.77",
        }
        exception_detail = " | ".join(sentinels.values()) + (
            r" | C:\private\turnstile.py SELECT secret FROM provider_state "
            "Traceback TURNSTILE_EXCEPTION_CANARY"
        )
        resolve_config.return_value = SimpleNamespace(
            enabled=True,
            ready=True,
            secret=sentinels["secret"],
        )
        post.side_effect = requests.RequestException(exception_detail)

        self.assertFalse(
            verify_turnstile(
                sentinels["token"],
                remote_ip=sentinels["remote_ip"],
            )
        )

        warning.assert_called_once_with(
            "turnstile_verification_failed",
            extra={
                "animemo_stage": "turnstile_siteverify",
                "animemo_exception_class": "RequestException",
            },
        )
        logged = repr(warning.call_args)
        for sentinel in sentinels.values():
            self.assertNotIn(sentinel, logged)
        self.assertNotIn("TURNSTILE_EXCEPTION_CANARY", logged)
        self.assertNotIn("exc_info", warning.call_args.kwargs)
        self.assertNotIn("stack_info", warning.call_args.kwargs)
