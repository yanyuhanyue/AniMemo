from __future__ import annotations

from io import StringIO

from django.core.management import BaseCommand, CommandError, call_command


TASKS = (
    "purge_expired_pending_registrations",
    "purge_expired_revoked_tokens",
    "cleanup_external_account_sessions",
    "cleanup_integration_events",
    "refresh_media_storage_usage",
    "audit_orphan_media",
)


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
            output = StringIO()
            try:
                call_command(task, stdout=output, stderr=output)
            except Exception as error:  # maintenance is isolated per task
                failures.append(task)
                self.stdout.write(self.style.ERROR(f"FAIL {task}: {error}"))
                continue
            self.stdout.write(self.style.SUCCESS(f"PASS {task}"))
            text = output.getvalue().strip()
            if text:
                self.stdout.write(text)
        if failures:
            raise CommandError("维护任务失败：" + ", ".join(failures))
