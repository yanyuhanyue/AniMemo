from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from plugin_host.installer import PluginPackageInstaller
from plugin_host.models import PluginDeployment, PluginProject, PluginVersion
from plugin_host.official_packages import OFFICIAL_PLUGIN_SLUGS, build_official_package
from plugin_host.services import store_package_blob


class Command(BaseCommand):
    help = "Register and, when a superuser exists, deploy bundled official plugins."

    def handle(self, *args, **options):
        source_base = Path(settings.BASE_DIR).parent / "plugins"
        actor = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
        for slug in OFFICIAL_PLUGIN_SLUGS:
            source_root = source_base / slug
            if not (source_root / "manifest.json").is_file():
                self.stdout.write(self.style.WARNING(f"Official plugin source missing: {slug}"))
                continue
            blob, inspected, _, _ = store_package_blob(build_official_package(source_root))
            manifest = inspected["manifest"]
            project, _ = PluginProject.objects.get_or_create(
                plugin_id=manifest["id"],
                defaults={
                    "slug": manifest["slug"], "name": manifest["name"], "description": manifest["description"],
                    "installation_mode": manifest["installationMode"],
                    "owner": actor,
                },
            )
            if project.slug != manifest["slug"]:
                raise RuntimeError(f"Official plugin id/slug conflict: {manifest['id']}")
            project_updates = []
            for field, value in (
                ("name", manifest["name"]),
                ("description", manifest["description"]),
                ("installation_mode", manifest["installationMode"]),
            ):
                if getattr(project, field) != value:
                    setattr(project, field, value)
                    project_updates.append(field)
            if project.owner_id is None and actor is not None:
                project.owner = actor
                project_updates.append("owner")
            if project_updates:
                project.save(update_fields=[*project_updates, "updated_at"])
            version, _ = PluginVersion.objects.get_or_create(
                plugin=project,
                version=manifest["version"],
                defaults={
                    "package_blob": blob, "manifest_snapshot": manifest,
                    "runtime_types": manifest.get("runtimes") or [],
                    "review_status": PluginVersion.ReviewStatus.APPROVED,
                    "created_by": actor,
                },
            )
            if version.package_blob_id != blob.pk:
                raise RuntimeError(f"Official immutable version SHA changed: {project.slug} {version.version}")
            if version.review_status != PluginVersion.ReviewStatus.APPROVED:
                version.review_status = PluginVersion.ReviewStatus.APPROVED
                version.save(update_fields=["review_status"])
            current_id = PluginDeployment.objects.filter(plugin=project).values_list("current_version_id", flat=True).first()
            if current_id == version.pk:
                self.stdout.write(f"Official plugin already current: {project.slug} {version.version}")
                continue
            if actor is None and "backend" in set(version.runtime_types or []):
                self.stdout.write(self.style.WARNING(f"Registered {project.slug} {version.version}; publish waits for a superuser."))
                continue
            PluginPackageInstaller().publish(version, actor=actor)
            self.stdout.write(self.style.SUCCESS(f"Published official plugin: {project.slug} {version.version}"))
