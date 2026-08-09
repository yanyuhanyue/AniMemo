from __future__ import annotations

import json

from django.core.management import BaseCommand, CommandError
from django.db import connection
from django.db.models import Count, Max

from accounts.models import User

from journal.models import JournalEntry


class Command(BaseCommand):
    help = "输出代表性 JournalEntry 列表查询的 EXPLAIN 计划；只读，不修改索引。"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=24, help="代表性列表大小，默认 24。")
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--user-id", type=int, help="要分析的用户主键。")
        scope.add_argument("--username", help="要分析的用户名；不会输出用户名或邮箱。")
        parser.add_argument("--format", choices=("text", "json"), default="text")

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 500:
            raise CommandError("--limit 必须介于 1 和 500 之间。")
        if options.get("user_id") is not None:
            user_id = options["user_id"]
            if not User.objects.filter(pk=user_id).exists():
                raise CommandError("指定用户不存在。")
        else:
            user = User.objects.filter(username=options["username"]).only("id").first()
            if user is None:
                raise CommandError("指定用户不存在。")
            user_id = user.pk
        queryset = (
            JournalEntry.objects.filter(user_id=user_id, deleted_at__isnull=True)
            .select_related("user")
            .annotate(
                watch_history_count=Count("watch_history_records", distinct=True),
                last_watched_on=Max("watch_history_records__watched_on"),
            )
            .prefetch_related("external_identities")
            .order_by("-updated_at", "-id")[:limit]
        )
        sql, params = queryset.query.sql_with_params()
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("EXPLAIN (FORMAT JSON) " + sql, params)
                plan = cursor.fetchone()[0]
            else:
                cursor.execute("EXPLAIN QUERY PLAN " + sql, params)
                plan = [list(row) for row in cursor.fetchall()]
        result = {
            "database": connection.vendor,
            "limit": limit,
            "user_id": user_id,
            "query": "journal_entries active list ordered by updated_at DESC, id DESC",
            "plan": plan,
            "index_change": "NOT NEEDED unless this plan shows a regression",
        }
        if options["format"] == "json":
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return
        self.stdout.write(f"database: {result['database']}")
        self.stdout.write(f"limit: {limit}")
        self.stdout.write(f"user_id: {user_id}")
        self.stdout.write(f"query: {result['query']}")
        self.stdout.write("plan:")
        self.stdout.write(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        self.stdout.write(f"index_change: {result['index_change']}")
