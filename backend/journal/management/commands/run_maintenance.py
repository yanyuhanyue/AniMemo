from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

from django.core.management import BaseCommand, CommandError

_MANAGE_PY = Path(__file__).resolve().parents[3] / "manage.py"
_TASK_COMMANDS = {
    task: (
        sys.executable,
        "-B",
        str(_MANAGE_PY),
        task,
        "--no-color",
    )
    for task in (
        "purge_expired_pending_registrations",
        "purge_expired_revoked_tokens",
        "cleanup_external_account_sessions",
        "cleanup_integration_events",
        "cleanup_watch_history_import_batches",
        "reconcile_media_write_reservations",
        "refresh_media_storage_usage",
        "audit_orphan_media",
    )
}
TASKS = tuple(_TASK_COMMANDS)

FAILURE_CODE = "maintenance_task_failed"


class Command(BaseCommand):
    help = "按标准入口执行 AniMemo 的非破坏性维护任务。"

    def add_arguments(self, parser):
        parser.add_argument("--task", action="append", choices=TASKS, help="只运行指定任务，可重复传入。")
        parser.add_argument("--list", action="store_true", dest="list_tasks", help="列出可用任务后退出。")

    def handle(self, *args, **options):
        if options["list_tasks"]:
            self.stdout.write("\n".join(TASKS))
            return
        selected = options["task"] or list(TASKS)
        failures = []
        for task in selected:
            try:
                completed = subprocess.run(
                    _TASK_COMMANDS[task],
                    cwd=_MANAGE_PY.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    check=False,
                )
            except Exception:
                return_code = -1
            else:
                return_code = completed.returncode
            if return_code != 0:
                correlation_id = secrets.token_hex(16)
                failure = (
                    f"{task} code={FAILURE_CODE} "
                    f"correlation_id={correlation_id}"
                )
                failures.append(failure)
                self.stdout.write(self.style.ERROR(f"FAIL {failure}"))
                continue
            self.stdout.write(self.style.SUCCESS(f"PASS {task}"))
        if failures:
            raise CommandError("维护任务失败：" + "; ".join(failures)) from None
