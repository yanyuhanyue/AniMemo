from django.core.management.base import BaseCommand

from plugin_host.registry import discover_plugins


class Command(BaseCommand):
    help = "列出已启用插件声明的 Python 依赖；仅用于构建，不会安装依赖。"

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="同时列出停用插件。")

    def handle(self, *args, **options):
        found = False
        for plugin in discover_plugins():
            if not options["all"] and not plugin.get("effective_enabled"):
                continue
            dependencies = plugin.get("backend", {}).get("pythonDependencies") or []
            for dependency in dependencies:
                found = True
                self.stdout.write(f"{plugin['slug']}: {dependency}")
        if not found:
            self.stdout.write("未发现插件 Python 依赖。")
