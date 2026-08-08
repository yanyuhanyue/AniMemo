from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from plugin_host.installer import PluginPackageInstaller
from plugin_host.models import PluginDeployment, PluginProject, PluginVersion
from plugin_host.official_packages import (
    OFFICIAL_PLUGIN_SLUGS,
    build_official_package,
    canonical_content_digest_from_descriptor,
)
from plugin_host.package import LocalPluginPackageStorage, PluginPackageError, inspect_package
from plugin_host.services import store_package_blob


def _existing_official_content_digest(version):
    blob = version.package_blob
    storage = LocalPluginPackageStorage(settings.PLUGIN_ROOT)
    expected_path = storage.package_path(blob.sha256)
    expected_storage_path = expected_path.relative_to(storage.root).as_posix()
    if blob.storage_path != expected_storage_path:
        raise RuntimeError(
            f"Official plugin historical PackageBlob storage path mismatch: {version.plugin.slug} {version.version}"
        )
    if not expected_path.is_file():
        raise RuntimeError(
            f"Official plugin historical package is missing from CAS: {version.plugin.slug} {version.version} {blob.sha256}"
        )
    try:
        payload = expected_path.read_bytes()
        inspected = inspect_package(payload)
    except (OSError, PluginPackageError) as error:
        raise RuntimeError(
            f"Official plugin historical package is corrupt: {version.plugin.slug} {version.version}: {error}"
        ) from error
    if len(payload) != blob.size_bytes or inspected["sha256"] != blob.sha256:
        raise RuntimeError(
            f"Official plugin historical PackageBlob integrity mismatch: {version.plugin.slug} {version.version} {blob.sha256}"
        )
    manifest = inspected["manifest"]
    if (
        manifest != version.manifest_snapshot
        or manifest.get("id") != version.plugin.plugin_id
        or manifest.get("slug") != version.plugin.slug
        or manifest.get("version") != version.version
    ):
        raise RuntimeError(
            f"Official plugin historical package metadata mismatch: {version.plugin.slug} {version.version}"
        )
    return canonical_content_digest_from_descriptor(inspected["files"])


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
            package_payload = build_official_package(source_root)
            inspected = inspect_package(package_payload)
            manifest = inspected["manifest"]
            current_content_digest = canonical_content_digest_from_descriptor(inspected["files"])
            project = PluginProject.objects.filter(plugin_id=manifest["id"]).first()
            if project is not None and project.slug != manifest["slug"]:
                raise RuntimeError(f"Official plugin id/slug conflict: {manifest['id']}")
            version = None
            if project is not None:
                version = (
                    PluginVersion.objects.select_related("plugin", "package_blob")
                    .filter(plugin=project, version=manifest["version"])
                    .first()
                )
            if version is not None:
                existing_content_digest = _existing_official_content_digest(version)
                if existing_content_digest != current_content_digest:
                    raise RuntimeError(
                        "Official plugin immutable content mismatch.\n"
                        f"slug: {project.slug}\n"
                        f"version: {version.version}\n"
                        f"existing content digest: {existing_content_digest}\n"
                        f"current content digest: {current_content_digest}\n"
                        "Do not rewrite an already-published official plugin version."
                    )
                blob = version.package_blob
            else:
                blob, _, _, _ = store_package_blob(package_payload)
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
            if version is None:
                version, created = PluginVersion.objects.get_or_create(
                    plugin=project,
                    version=manifest["version"],
                    defaults={
                        "package_blob": blob, "manifest_snapshot": manifest,
                        "runtime_types": manifest.get("runtimes") or [],
                        "review_status": PluginVersion.ReviewStatus.APPROVED,
                        "created_by": actor,
                    },
                )
                if not created and version.package_blob_id != blob.pk:
                    existing_content_digest = _existing_official_content_digest(
                        PluginVersion.objects.select_related("plugin", "package_blob").get(pk=version.pk)
                    )
                    if existing_content_digest != current_content_digest:
                        raise RuntimeError(
                            f"Official plugin immutable content mismatch after concurrent registration: {project.slug} {version.version}"
                        )
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
