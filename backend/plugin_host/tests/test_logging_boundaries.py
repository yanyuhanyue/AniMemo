import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from plugin_host.hooks import HookRegistry, run_registration_hook
from plugin_host.sdk.logging import PluginLoggerAdapter, get_plugin_logger

PRIVATE_DIAGNOSTIC = (
    r"C:\private\plugin.py SELECT secret FROM provider_state "
    "token=TOKEN_CANARY signed_url=https://private.invalid/?sig=SIGNED_URL_CANARY "
    "command=sudo-private username=operator Traceback PLUGIN_PRIVATE_CANARY"
)
HOSTILE_EXCEPTION_NAME = "RuntimeError\\nC:\\private\\token_TOKEN_CANARY"
HostileHookError = type(HOSTILE_EXCEPTION_NAME, (RuntimeError,), {})


class PluginHostLoggingBoundaryTests(SimpleTestCase):
    @staticmethod
    def _failing_callback(*_args, **_kwargs):
        raise RuntimeError(PRIVATE_DIAGNOSTIC)

    @patch.object(HookRegistry, "_registration_is_authorized", return_value=True)
    @patch("plugin_host.hooks.logger.warning")
    def test_action_and_filter_failures_log_only_closed_metadata(
        self,
        warning,
        _authorized,
    ):
        registry = HookRegistry()
        owner = ("safe-plugin", "1.0.0", "runtime")
        registry.register("journal.after_create", self._failing_callback, owner)
        registry.register("user.before_delete", self._failing_callback, owner)

        self.assertEqual(registry.run_hook("journal.after_create", SimpleNamespace()), [])
        with self.assertRaises(RuntimeError):
            registry.run_filter("user.before_delete", object(), SimpleNamespace())

        self.assertEqual(
            warning.call_args_list[0].args,
            ("plugin_hook_callback_failed",),
        )
        self.assertEqual(
            warning.call_args_list[0].kwargs,
            {
                "extra": {
                    "animemo_stage": "plugin_hook_callback",
                    "plugin": "safe-plugin",
                    "animemo_exception_class": "RuntimeError",
                }
            },
        )
        self.assertEqual(
            warning.call_args_list[1].args,
            ("plugin_filter_callback_failed",),
        )
        self.assertEqual(
            warning.call_args_list[1].kwargs,
            {
                "extra": {
                    "animemo_stage": "plugin_filter_callback",
                    "plugin": "safe-plugin",
                    "animemo_exception_class": "RuntimeError",
                }
            },
        )
        self.assertNotIn(PRIVATE_DIAGNOSTIC, repr(warning.call_args_list))

    @patch(
        "plugin_host.hooks.run_hook",
        side_effect=HostileHookError(PRIVATE_DIAGNOSTIC),
    )
    @patch("plugin_host.hooks.logger.warning")
    def test_registration_fail_open_log_discards_hook_and_exception_text(
        self,
        warning,
        _run_hook,
    ):
        run_registration_hook("registration.after_complete")

        warning.assert_called_once_with(
            "registration_hook_failed_open",
            extra={
                "animemo_stage": "registration_hook_fail_open",
                "animemo_exception_class": "PluginHookError",
            },
        )
        self.assertNotIn(PRIVATE_DIAGNOSTIC, repr(warning.call_args))
        self.assertNotIn(HOSTILE_EXCEPTION_NAME, repr(warning.call_args))

    @patch.object(HookRegistry, "_registration_is_authorized", return_value=True)
    @patch("plugin_host.hooks.logger.warning")
    def test_hostile_exception_class_name_is_collapsed_to_fixed_metadata(
        self,
        warning,
        _authorized,
    ):
        def fail(*_args, **_kwargs):
            raise HostileHookError(PRIVATE_DIAGNOSTIC)

        registry = HookRegistry()
        owner = ("safe-plugin", "1.0.0", "runtime")
        registry.register("journal.after_create", fail, owner)
        registry.register("user.before_delete", fail, owner)

        self.assertEqual(registry.run_hook("journal.after_create", SimpleNamespace()), [])
        with self.assertRaises(HostileHookError):
            registry.run_filter("user.before_delete", object(), SimpleNamespace())

        for logged in warning.call_args_list:
            self.assertEqual(
                logged.kwargs["extra"]["animemo_exception_class"],
                "PluginHookError",
            )
        logged_text = repr(warning.call_args_list)
        self.assertNotIn(HOSTILE_EXCEPTION_NAME, logged_text)
        self.assertNotIn("TOKEN_CANARY", logged_text)
        self.assertNotIn(PRIVATE_DIAGNOSTIC, logged_text)

    def test_plugin_logger_discards_message_args_extra_and_trace_options(self):
        underlying = Mock(spec=logging.Logger)
        underlying.isEnabledFor.return_value = True
        adapter = PluginLoggerAdapter(underlying, {"plugin": "safe-plugin"})

        adapter.error(
            PRIVATE_DIAGNOSTIC + " %s",
            PRIVATE_DIAGNOSTIC,
            extra={"token": PRIVATE_DIAGNOSTIC, "plugin": "attacker"},
            exc_info=(RuntimeError, RuntimeError(PRIVATE_DIAGNOSTIC), None),
            stack_info=True,
            stacklevel=99,
        )

        underlying.log.assert_called_once_with(
            logging.ERROR,
            "plugin_log_event",
            extra={
                "animemo_stage": "plugin_sdk_log",
                "plugin": "safe-plugin",
            },
        )
        self.assertNotIn(PRIVATE_DIAGNOSTIC, repr(underlying.log.call_args))

    @patch("plugin_host.sdk.logging.logging.getLogger")
    def test_plugin_logger_rejects_unvalidated_slug(self, get_logger):
        with self.assertRaises(ValueError):
            get_plugin_logger("unsafe/plugin?token=SLUG_CANARY")
        get_logger.assert_not_called()
