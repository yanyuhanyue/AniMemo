import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Reset only the obsolete Plugin Platform v2 schema before the clean v3 0001 migration."

    def handle(self, *args, **options):
        tables = set(connection.introspection.table_names())
        old_table = "plugin_host_plugininstallation"
        new_table = "plugin_host_pluginproject"
        if old_table not in tables or new_table in tables:
            self.stdout.write("Plugin Platform v3 schema preparation: no reset required.")
            return
        plugin_tables = sorted(table for table in tables if table.startswith("plugin_host_"))
        if not plugin_tables:
            return
        quoted = ", ".join(connection.ops.quote_name(table) for table in plugin_tables)
        with transaction.atomic(), connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(f"DROP TABLE {quoted} CASCADE")
            elif connection.vendor == "sqlite":
                for table in plugin_tables:
                    cursor.execute(f"DROP TABLE {connection.ops.quote_name(table)}")
            else:
                raise RuntimeError(f"Unsupported database vendor for scoped plugin reset: {connection.vendor}")
            cursor.execute("DELETE FROM django_migrations WHERE app = %s", ["plugin_host"])
        package_root = Path(settings.PLUGIN_ROOT) / "packages"
        if package_root.is_dir():
            for child in package_root.iterdir():
                if child.name != "sha256":
                    shutil.rmtree(child, ignore_errors=True)
        self.stdout.write(self.style.SUCCESS(f"Reset {len(plugin_tables)} legacy plugin_host tables only."))
