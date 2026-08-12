import json

from django.core.management.base import BaseCommand

from plugin_host.models import PluginDeployment


class Command(BaseCommand):
    help = "Print enabled Plugin Manifest v2 sdkApi values as JSON for the host updater."

    def handle(self, *args, **options):
        values = set()
        deployments = PluginDeployment.objects.filter(enabled=True).select_related("current_version")
        for deployment in deployments:
            manifest = deployment.current_version.manifest_snapshot
            values.add(int(manifest["sdkApi"]))
        self.stdout.write(json.dumps(sorted(values)))
