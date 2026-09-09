"""Controlled local-file adapter to the same durable portable restore service."""
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from journal.data_bundle.restore import (
    BundleRestoreError,
    cancel_restore,
    commit_restore,
    create_restore,
    upload_chunk,
    validate_restore,
)


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class Command(BaseCommand):
    help = "Validate a local Data Bundle using durable sessions; --commit applies atomically to the explicit empty owner."

    def add_arguments(self, parser):
        parser.add_argument("--owner-id", type=int, required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--idempotency-key", type=uuid.UUID, required=True)
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.is_absolute():
            raise CommandError("bundle_restore_input_requires_absolute_path")
        try:
            owner = get_user_model().objects.get(pk=options["owner_id"], is_active=True)
        except get_user_model().DoesNotExist as error:
            raise CommandError("owner_required") from error
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or getattr(before, "st_file_attributes", 0) & 0x400:
                raise CommandError("bundle_restore_input_requires_regular_file")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                if identity(os.fstat(source.fileno())) != identity(before):
                    raise CommandError("bundle_restore_input_changed")
                digest = hashlib.sha256()
                while chunk := source.read(settings.BUNDLE_RESTORE_CHUNK_BYTES):
                    digest.update(chunk)
                if identity(os.fstat(source.fileno())) != identity(before):
                    raise CommandError("bundle_restore_input_changed")
                state = create_restore(user=owner, payload={
                    "idempotency_key": str(options["idempotency_key"]), "expected_bytes": before.st_size,
                    "sha256": digest.hexdigest(), "schema_version": 1,
                })
                self.report(state)
                if state["state"] == "receiving":
                    source.seek(state["received_bytes"])
                    while chunk := source.read(settings.BUNDLE_RESTORE_CHUNK_BYTES):
                        state = upload_chunk(user=owner, session_id=state["id"], generation=state["generation"],
                                             offset=state["received_bytes"], data=chunk)
                    if identity(os.fstat(source.fileno())) != identity(before):
                        cancel_restore(user=owner, session_id=state["id"], generation=state["generation"])
                        raise CommandError("bundle_restore_input_changed")
                    state = validate_restore(user=owner, session_id=state["id"], generation=state["generation"])
                    self.report(state)
                if state["state"] == "ready" and options["commit"]:
                    state = commit_restore(user=owner, session_id=state["id"], generation=state["generation"])
                    self.report(state)
                if state["state"] not in ("ready", "completed"):
                    raise CommandError(state["error_code"] or "bundle_restore_state")
        except BundleRestoreError as error:
            raise CommandError(error.code) from error
        except OSError as error:
            raise CommandError("bundle_restore_input_unreadable") from error

    def report(self, state):
        # Same bounded state/receipt shape as HTTP; never print raw file content.
        self.stdout.write(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
