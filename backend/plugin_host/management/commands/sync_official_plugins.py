import os
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from plugin_host.filesystem_security import (
    PluginFilesystemSecurityError,
    filesystem_diagnostic_code,
)
from plugin_host.installer import PluginPackageInstaller
from plugin_host.models import PluginDeployment, PluginProject, PluginVersion
from plugin_host.official_packages import (
    OFFICIAL_PLUGIN_SLUGS,
    build_official_package,
    canonical_content_digest_from_descriptor,
)
from plugin_host.package import (
    LocalPluginPackageStorage,
    PluginPackageError,
    inspect_package,
)
from plugin_host.services import store_package_blob

LEGACY_OFFICIAL_PLUGIN_IDS = {
    "watch-history-importer": {"com.anime-journal.watch-history-importer"},
}

_PUBLIC_FAILURE_CODE = "official_plugin_sync_failed"


def _command_failure():
    return CommandError(
        f"{_PUBLIC_FAILURE_CODE} correlation_id={secrets.token_hex(16)}"
    )


def _diagnostic_class(error):
    if isinstance(error, PluginFilesystemSecurityError):
        return "filesystem_security"
    if isinstance(error, PluginPackageError):
        return "package"
    if isinstance(error, DatabaseError):
        return "database"
    if isinstance(error, OSError):
        return "os"
    if isinstance(error, CommandError):
        return "command"
    return "unexpected"


def _diagnostic_reason(error):
    if isinstance(error, PluginFilesystemSecurityError):
        return filesystem_diagnostic_code(error)
    return "none"


def _existing_official_content_digest(version):
    blob = version.package_blob
    storage = LocalPluginPackageStorage(settings.PLUGIN_ROOT)
    expected_path = storage.package_path(blob.sha256)
    expected_storage_path = expected_path.relative_to(storage.root).as_posix()
    if blob.storage_path != expected_storage_path:
        raise _command_failure() from None
    if not expected_path.is_file():
        raise _command_failure() from None
    payload = expected_path.read_bytes()
    inspected = inspect_package(payload)
    if len(payload) != blob.size_bytes or inspected["sha256"] != blob.sha256:
        raise _command_failure() from None
    manifest = inspected["manifest"]
    if (
        manifest != version.manifest_snapshot
        or manifest.get("id") != version.plugin.plugin_id
        or manifest.get("slug") != version.plugin.slug
        or manifest.get("version") != version.version
    ):
        raise _command_failure() from None
    return canonical_content_digest_from_descriptor(inspected["files"])


class Command(BaseCommand):
    help = "Register and, when a superuser exists, deploy bundled official plugins."

    def handle(self, *args, **options):
        self._failure_stage = "entry"
        try:
            return self._handle(*args, **options)
        # The outer boundary intentionally collapses every internal failure to the
        # same public error; CI receives only the allowlisted class below.
        except Exception as error:  # noqa: BLE001
            if settings.DEBUG and os.getenv("CI", "").strip().casefold() == "true":
                self.stdout.write(
                    "official_plugin_sync_internal_failure "
                    f"stage={self._failure_stage} "
                    f"class={_diagnostic_class(error)} "
                    f"reason={_diagnostic_reason(error)}"
                )
            raise _command_failure() from None

    def _handle(self, *args, **options):
        source_base = Path(settings.BASE_DIR).parent / "plugins"
        self._failure_stage = "actor_lookup"
        actor = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
        for slug in OFFICIAL_PLUGIN_SLUGS:
            self._failure_stage = "source_identity"
            source_root = source_base / slug
            if not (source_root / "manifest.json").is_file():
                self.stdout.write(self.style.WARNING(f"Official plugin source missing: {slug}"))
                continue
            self._failure_stage = "package_build"
            package_payload = build_official_package(source_root)
            inspected = inspect_package(package_payload)
            manifest = inspected["manifest"]
            current_content_digest = canonical_content_digest_from_descriptor(inspected["files"])
            self._failure_stage = "project_lookup"
            project_by_id = PluginProject.objects.filter(plugin_id=manifest["id"]).first()
            project_by_slug = PluginProject.objects.filter(slug=manifest["slug"]).first()
            if (
                project_by_id is not None
                and project_by_slug is not None
                and project_by_id.pk != project_by_slug.pk
            ):
                raise _command_failure() from None
            if project_by_id is not None and project_by_id.slug != manifest["slug"]:
                raise _command_failure() from None
            project = project_by_id or project_by_slug
            if project is not None and project.plugin_id != manifest["id"]:
                legacy_ids = LEGACY_OFFICIAL_PLUGIN_IDS.get(manifest["slug"], set())
                if project.plugin_id not in legacy_ids:
                    raise _command_failure() from None
                project.plugin_id = manifest["id"]
                project.save(update_fields=["plugin_id", "updated_at"])
            version = None
            if project is not None:
                version = (
                    PluginVersion.objects.select_related("plugin", "package_blob")
                    .filter(plugin=project, version=manifest["version"])
                    .first()
                )
            if version is not None:
                self._failure_stage = "existing_package_verify"
                existing_content_digest = _existing_official_content_digest(version)
                if existing_content_digest != current_content_digest:
                    raise _command_failure() from None
                blob = version.package_blob
            else:
                self._failure_stage = "package_store"
                blob, _, _, _ = store_package_blob(package_payload)
            self._failure_stage = "project_upsert"
            project, _ = PluginProject.objects.get_or_create(
                plugin_id=manifest["id"],
                defaults={
                    "slug": manifest["slug"], "name": manifest["name"], "description": manifest["description"],
                    "installation_mode": manifest["installationMode"],
                    "owner": actor,
                },
            )
            if project.slug != manifest["slug"]:
                raise _command_failure() from None
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
                self._failure_stage = "version_upsert"
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
                        raise _command_failure() from None
            self._failure_stage = "version_status_update"
            if version.review_status != PluginVersion.ReviewStatus.APPROVED:
                version.review_status = PluginVersion.ReviewStatus.APPROVED
                version.save(update_fields=["review_status"])
            self._failure_stage = "deployment_lookup"
            current_id = PluginDeployment.objects.filter(plugin=project).values_list("current_version_id", flat=True).first()
            if current_id == version.pk:
                self.stdout.write(f"Official plugin already current: {project.slug} {version.version}")
                continue
            if actor is None and "backend" in set(version.runtime_types or []):
                self.stdout.write(self.style.WARNING(f"Registered {project.slug} {version.version}; publish waits for a superuser."))
                continue
            self._failure_stage = "publish"
            PluginPackageInstaller().publish(version, actor=actor)
            self.stdout.write(self.style.SUCCESS(f"Published official plugin: {project.slug} {version.version}"))
        self._failure_stage = "complete"
