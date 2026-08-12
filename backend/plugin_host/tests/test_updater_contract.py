from __future__ import annotations

import io
import json

from django.core.management import call_command
from django.test import TestCase

from plugin_host.models import (
    PluginDeployment,
    PluginPackageBlob,
    PluginProject,
    PluginVersion,
)


class EnabledPluginApiCommandTests(TestCase):
    def test_no_enabled_plugins_reports_an_empty_api_set(self):
        output = io.StringIO()

        call_command("list_enabled_plugin_apis", stdout=output)

        self.assertEqual(json.loads(output.getvalue()), [])

    def test_only_enabled_deployments_contribute_sdk_apis(self):
        blob = PluginPackageBlob.objects.create(sha256="a" * 64, size_bytes=1, storage_path="sha256/a.ajplugin")
        for index, (sdk_api, enabled) in enumerate([(1, False), (2, True)], start=1):
            project = PluginProject.objects.create(
                plugin_id=f"com.example.updater-{index}",
                slug=f"updater-{index}",
                name=f"Updater {index}",
                description="Updater contract fixture",
            )
            version = PluginVersion.objects.create(
                plugin=project,
                version="1.0.0",
                package_blob=blob,
                manifest_snapshot={"sdkApi": sdk_api},
            )
            PluginDeployment.objects.create(
                plugin=project,
                current_version=version,
                enabled=enabled,
                healthy=True,
            )
        output = io.StringIO()

        call_command("list_enabled_plugin_apis", stdout=output)

        self.assertEqual(json.loads(output.getvalue()), [2])
