from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .image_security import delete_replaced_file, schedule_file_delete

PRIVATE_DIAGNOSTIC = (
    r"C:\private\image.webp SELECT secret FROM provider_state "
    "token=TOKEN_CANARY signed_url=https://private.invalid/?sig=SIGNED_URL_CANARY "
    "command=sudo-private username=operator Traceback IMAGE_PRIVATE_CANARY"
)


class ImageCleanupLoggingBoundaryTests(SimpleTestCase):
    @patch("journal.image_security.transaction.on_commit", side_effect=lambda callback: callback())
    @patch("journal.image_security.logger.warning")
    def test_replaced_file_cleanup_log_discards_name_storage_and_exception(
        self,
        warning,
        _on_commit,
    ):
        storage = Mock()
        storage.delete.side_effect = RuntimeError(PRIVATE_DIAGNOSTIC)
        previous = SimpleNamespace(name=PRIVATE_DIAGNOSTIC, storage=storage)
        current = SimpleNamespace(name="different.webp")

        delete_replaced_file(previous, current)

        warning.assert_called_once_with(
            "image_cleanup_failed",
            extra={
                "animemo_stage": "replaced_image_delete",
                "animemo_exception_class": "RuntimeError",
            },
        )
        self.assertNotIn(PRIVATE_DIAGNOSTIC, repr(warning.call_args))

    @patch("journal.image_security.transaction.on_commit", side_effect=lambda callback: callback())
    @patch("journal.image_security.logger.warning")
    def test_model_file_cleanup_log_discards_object_identity_and_exception(
        self,
        warning,
        _on_commit,
    ):
        storage = Mock()
        storage.delete.side_effect = RuntimeError(PRIVATE_DIAGNOSTIC)
        file_field = SimpleNamespace(name=PRIVATE_DIAGNOSTIC, storage=storage)

        schedule_file_delete(
            file_field,
            model_name=PRIVATE_DIAGNOSTIC,
            object_id=PRIVATE_DIAGNOSTIC,
        )

        warning.assert_called_once_with(
            "image_cleanup_failed",
            extra={
                "animemo_stage": "model_image_delete",
                "animemo_exception_class": "RuntimeError",
            },
        )
        self.assertNotIn(PRIVATE_DIAGNOSTIC, repr(warning.call_args))
