import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand

from site_config.models import SiteSettings
from journal.models import Column, JournalEntry, UserSettings


class Command(BaseCommand):
    help = "列出未被数据库引用的本地媒体文件；删除必须显式使用 --delete。"

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true")

    def handle(self, *args, **options):
        if not isinstance(default_storage, FileSystemStorage):
            self.stdout.write("当前媒体存储不是本地 FileSystemStorage，命令只执行数据库引用审计，不枚举远程对象。")
            return

        referenced = set()
        for queryset, field_name in (
            (UserSettings.objects.all(), "avatar"),
            (JournalEntry.objects.all(), "poster_file"),
            (Column.objects.all(), "cover"),
            (SiteSettings.objects.all(), "site_avatar"),
        ):
            referenced.update(
                name for name in queryset.values_list(field_name, flat=True) if name
            )

        media_root = os.fspath(settings.MEDIA_ROOT)
        orphaned = []
        if os.path.isdir(media_root):
            for root, _dirs, files in os.walk(media_root):
                for filename in files:
                    absolute = os.path.join(root, filename)
                    relative = os.path.relpath(absolute, media_root).replace(os.sep, "/")
                    if relative not in referenced:
                        orphaned.append(relative)

        for name in orphaned:
            self.stdout.write(name)
            if options["delete"]:
                default_storage.delete(name)
        action = "已删除" if options["delete"] else "发现"
        self.stdout.write(self.style.SUCCESS(f"{action} {len(orphaned)} 个孤立媒体文件。"))
