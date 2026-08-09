from __future__ import annotations

import os
import shutil
import time
from io import BytesIO
from pathlib import PurePosixPath
from uuid import uuid4
from zipfile import ZipFile

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from packaging.version import Version

from .models import PluginDeployment, PluginVersion
from .package import LocalPluginPackageStorage, PluginPackageError, inspect_package
from .runtime import RuntimeLoadError, runtime_registry


class PluginInstallError(PluginPackageError):
    pass


class _PluginFilesystemLock:
    def __init__(self, root, slug, timeout=10):
        self.path = root / ".locks" / f"{slug}.lock"
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"{os.getpid()}\n".encode("ascii"))
                return self
            except FileExistsError:
                try:
                    stale = time.time() - self.path.stat().st_mtime > 300
                except FileNotFoundError:
                    continue
                if stale:
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise PluginInstallError("同一插件正在执行另一项生命周期操作。")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, traceback):
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


class PluginPackageInstaller:
    """Deploys only reviewed immutable PluginVersion rows.

    Developer uploads never call this class. Loading Python is intentionally
    confined to publish/rollback, both trusted administrator operations.
    """

    def __init__(self, root=None):
        self.storage = LocalPluginPackageStorage(root or settings.PLUGIN_ROOT)

    @staticmethod
    def _extract(payload, inspected, destination):
        destination.mkdir(parents=True, exist_ok=False)
        with ZipFile(BytesIO(payload)) as archive:
            for entry in inspected["files"]:
                relative = PurePosixPath(entry["path"])
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(relative.as_posix()))

    def _assert_growth_allowed(self, expanded_bytes, *, peak_multiplier=2):
        self.storage.ensure()
        minimum = int(getattr(settings, "PLUGIN_MIN_FREE_DISK_MB", 2048)) * 1024 * 1024
        peak_growth = max(0, int(expanded_bytes)) * max(1, int(peak_multiplier))
        free = shutil.disk_usage(self.storage.root).free
        if free - peak_growth < minimum:
            raise PluginInstallError("插件存储空间不足，无法发布。")

    def _payload_for(self, plugin_version):
        path = self.storage.root / plugin_version.package_blob.storage_path
        if path != self.storage.package_path(plugin_version.package_blob.sha256):
            raise PluginInstallError("PackageBlob 存储路径不符合 CAS 规则。")
        if not path.is_file():
            raise PluginInstallError("插件 CAS 文件不存在。")
        payload = path.read_bytes()
        try:
            inspected = inspect_package(payload)
        except PluginPackageError as error:
            raise PluginInstallError(str(error)) from error
        if inspected["sha256"] != plugin_version.package_blob.sha256:
            raise PluginInstallError("插件 CAS 文件完整性校验失败。")
        manifest = inspected["manifest"]
        if manifest != plugin_version.manifest_snapshot:
            raise PluginInstallError("不可变版本的 Manifest 快照不一致。")
        return payload, inspected

    def publish(self, plugin_version, *, actor=None):
        plugin_version = PluginVersion.objects.select_related("plugin", "package_blob").get(pk=plugin_version.pk)
        plugin = plugin_version.plugin
        if plugin_version.review_status != PluginVersion.ReviewStatus.APPROVED or plugin_version.revoked_at:
            raise PluginInstallError("只有审核通过且未撤销的版本可以发布。")
        if "backend" in set(plugin_version.runtime_types or []) and not getattr(actor, "is_superuser", False):
            raise PluginInstallError("包含 Backend Runtime 的版本只能由超级管理员发布。")

        slug = plugin.slug
        version = plugin_version.version
        with _PluginFilesystemLock(self.storage.root, slug):
            deployment = PluginDeployment.objects.select_related("current_version").filter(plugin=plugin).first()
            old_version = deployment.current_version if deployment else None
            if old_version and old_version.pk == plugin_version.pk:
                raise PluginInstallError("该版本已经是当前部署版本。")
            if deployment and deployment.enabled and deployment.healthy:
                runtime_registry.ensure_current(slug)

            payload, inspected = self._payload_for(plugin_version)
            declared_floor = str((inspected["manifest"].get("dataCompatibility") or {}).get("rollbackFloor") or "")
            current_floor = deployment.rollback_floor if deployment else ""
            effective_floor = max(
                (value for value in (current_floor, declared_floor) if value),
                key=Version,
                default="",
            )
            if effective_floor and Version(version) < Version(effective_floor):
                raise PluginInstallError(f"版本 {version} 低于数据兼容下限 {effective_floor}，不能发布。")
            expanded_bytes = sum(item["size"] for item in inspected["files"])
            # Publish keeps both the extracted staging tree and a temporary
            # runtime tree before the atomic replacement, so reserve 2x the
            # expanded package size in addition to the configured free floor.
            self._assert_growth_allowed(expanded_bytes, peak_multiplier=2)
            runtime_target = self.storage.runtime / slug / version
            staging = self.storage.staging / f"{slug}-{version}-{uuid4().hex}"
            runtime_temp = runtime_target.with_name(f".{version}.staged-{uuid4().hex}")
            previous_candidate = None
            final_candidate = None
            activated = False
            runtime_written = False
            committed = False
            was_enabled = True if deployment is None else deployment.enabled
            try:
                if runtime_target.exists():
                    shutil.rmtree(runtime_target, ignore_errors=True)
                self._extract(payload, inspected, staging)
                shutil.copytree(staging, runtime_temp)
                candidate = runtime_registry.load_candidate(runtime_temp, expected_slug=slug, expected_version=version)
                candidate.dispose()

                runtime_target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(runtime_temp, runtime_target)
                runtime_written = True
                final_candidate = runtime_registry.load_candidate(runtime_target, expected_slug=slug, expected_version=version)

                with runtime_registry.plugin_lock(slug):
                    with transaction.atomic():
                        locked_version = PluginVersion.objects.select_for_update().get(pk=plugin_version.pk)
                        if locked_version.review_status != PluginVersion.ReviewStatus.APPROVED or locked_version.revoked_at:
                            raise PluginInstallError("版本在发布期间被撤销或不再处于审核通过状态。")
                        locked = PluginDeployment.objects.select_for_update().filter(plugin=plugin).first()
                        locked_current_id = locked.current_version_id if locked else None
                        expected_id = old_version.pk if old_version else None
                        if locked_current_id != expected_id:
                            raise PluginInstallError("部署版本已被另一项操作修改。")
                        if was_enabled:
                            previous_candidate = runtime_registry.activate_candidate_locked(final_candidate)
                            activated = True
                        if locked is None:
                            locked = PluginDeployment(plugin=plugin, current_version=locked_version)
                        locked.previous_version = old_version
                        locked.current_version = locked_version
                        locked.enabled = was_enabled
                        locked.healthy = True
                        locked.status = PluginDeployment.Status.ENABLED if was_enabled else PluginDeployment.Status.DEPLOYED
                        locked.last_error = ""
                        locked.updated_by = actor
                        locked.disk_bytes = sum(path.stat().st_size for path in runtime_target.rglob("*") if path.is_file())
                        locked.rollback_floor = effective_floor
                        locked.save()
                        locked_version.published_at = timezone.now()
                        locked_version.save(update_fields=["published_at"])
                committed = True
                if not activated and final_candidate is not None:
                    final_candidate.dispose()
                runtime_registry.finalize_previous(previous_candidate)
                self.storage.retain_versions(
                    slug,
                    current=version,
                    previous=old_version.version if old_version else "",
                    keep=int(getattr(settings, "PLUGIN_KEEP_VERSIONS", 2)),
                )
                return {
                    "slug": slug,
                    "version": version,
                    "previous_version": old_version.version if old_version else "",
                }
            except Exception as error:
                if activated:
                    with runtime_registry.plugin_lock(slug):
                        runtime_registry.restore_candidate_locked(slug, previous_candidate)
                elif final_candidate is not None:
                    final_candidate.dispose()
                if not committed and runtime_written:
                    shutil.rmtree(runtime_target, ignore_errors=True)
                if isinstance(error, (PluginInstallError, RuntimeLoadError, PluginPackageError)):
                    raise PluginInstallError(str(error)) from error
                raise
            finally:
                shutil.rmtree(runtime_temp, ignore_errors=True)
                shutil.rmtree(staging, ignore_errors=True)

    def rollback(self, slug, *, actor=None):
        self.storage.ensure()
        with _PluginFilesystemLock(self.storage.root, slug):
            deployment = PluginDeployment.objects.select_related(
                "plugin", "current_version", "previous_version", "previous_version__package_blob"
            ).filter(plugin__slug=slug).first()
            if deployment is None or deployment.previous_version is None:
                raise PluginInstallError("没有可回滚的上一版本。")
            current_version = deployment.current_version
            target_version = deployment.previous_version
            if target_version.revoked_at:
                raise PluginInstallError("上一版本已撤销，不能回滚。")
            if deployment.rollback_floor and Version(target_version.version) < Version(deployment.rollback_floor):
                raise PluginInstallError(
                    f"目标版本 {target_version.version} 低于数据兼容下限 {deployment.rollback_floor}，不能回滚。"
                )
            if deployment.enabled and deployment.healthy:
                runtime_registry.ensure_current(slug)
            runtime_target = self.storage.runtime / slug / target_version.version
            if not runtime_target.is_dir():
                _, inspected = self._payload_for(target_version)
                expanded_bytes = sum(item["size"] for item in inspected["files"])
                self._assert_growth_allowed(expanded_bytes, peak_multiplier=2)
                self.storage.rollback(slug, target_version.version, target_version.package_blob.sha256)
            candidate = runtime_registry.load_candidate(runtime_target, expected_slug=slug, expected_version=target_version.version)
            previous_candidate = None
            activated = False
            try:
                with runtime_registry.plugin_lock(slug):
                    with transaction.atomic():
                        locked = PluginDeployment.objects.select_for_update().get(pk=deployment.pk)
                        if locked.current_version_id != current_version.pk or locked.previous_version_id != target_version.pk:
                            raise PluginInstallError("部署版本已被另一项操作修改。")
                        if locked.enabled:
                            previous_candidate = runtime_registry.activate_candidate_locked(candidate)
                            activated = True
                        locked.current_version = target_version
                        locked.previous_version = current_version
                        locked.healthy = True
                        locked.last_error = ""
                        locked.status = PluginDeployment.Status.ENABLED if locked.enabled else PluginDeployment.Status.DEPLOYED
                        locked.updated_by = actor
                        locked.disk_bytes = sum(path.stat().st_size for path in runtime_target.rglob("*") if path.is_file())
                        locked.save()
                if not activated:
                    candidate.dispose()
                runtime_registry.finalize_previous(previous_candidate)
                self.storage.retain_versions(slug, current=target_version.version, previous=current_version.version)
                return {"slug": slug, "version": target_version.version, "previous_version": current_version.version}
            except Exception:
                if activated:
                    with runtime_registry.plugin_lock(slug):
                        runtime_registry.restore_candidate_locked(slug, previous_candidate)
                else:
                    candidate.dispose()
                raise

    def set_enabled(self, slug, enabled, *, actor=None):
        deployment = PluginDeployment.objects.select_related("plugin", "current_version").filter(plugin__slug=slug).first()
        if deployment is None:
            raise PluginInstallError("插件尚未部署。")
        candidate = runtime_registry.load_installed_candidate(slug, deployment.current_version.version) if enabled else None
        previous = None
        with _PluginFilesystemLock(self.storage.root, slug), runtime_registry.plugin_lock(slug):
            try:
                with transaction.atomic():
                    locked = PluginDeployment.objects.select_for_update().get(pk=deployment.pk)
                    if locked.current_version_id != deployment.current_version_id:
                        raise PluginInstallError("部署版本已被另一项操作修改。")
                    if enabled:
                        previous = runtime_registry.activate_candidate_locked(candidate)
                        locked.healthy = True
                        locked.last_error = ""
                        locked.status = PluginDeployment.Status.ENABLED
                    else:
                        previous = runtime_registry._active.pop(slug, None)
                        if previous is not None:
                            previous.deactivate()
                        locked.status = PluginDeployment.Status.DEPLOYED
                    locked.enabled = bool(enabled)
                    locked.updated_by = actor
                    locked.save(update_fields=["enabled", "healthy", "last_error", "status", "updated_by", "updated_at"])
                if enabled:
                    runtime_registry.finalize_previous(previous)
                elif previous is not None:
                    previous.dispose()
                return locked
            except Exception:
                if enabled and candidate is not None:
                    runtime_registry.restore_candidate_locked(slug, previous)
                elif not enabled and previous is not None:
                    previous.activate()
                    runtime_registry._active[slug] = previous
                raise

    def revoke(self, plugin_version, *, actor=None):
        with transaction.atomic():
            locked_version = PluginVersion.objects.select_for_update().get(pk=plugin_version.pk)
            locked_version.review_status = PluginVersion.ReviewStatus.REVOKED
            locked_version.revoked_at = timezone.now()
            locked_version.save(update_fields=["review_status", "revoked_at"])
            deployment = PluginDeployment.objects.select_for_update().filter(current_version=locked_version).first()
            if deployment:
                deployment.enabled = False
                deployment.healthy = False
                deployment.status = PluginDeployment.Status.REVOKED
                deployment.last_error = "当前版本已被管理员撤销。"
                deployment.updated_by = actor
                deployment.save()
        if deployment:
            runtime_registry.unload(deployment.plugin.slug)
        return locked_version

    def cleanup(self, slug=None):
        from .services import garbage_collect_package_blobs

        self.storage.ensure()
        now = time.time()
        staging_grace = int(getattr(settings, "PLUGIN_STAGING_GC_GRACE_SECONDS", 3600))
        result = {
            "staging_removed": self.storage.cleanup_staging(older_than=now - staging_grace),
            "runtime_removed": [],
            "runtime_retained": {},
            "preview_removed": [],
            "package_blobs_removed": [],
            "orphan_files_removed": [],
            "missing_blob_files": [],
        }
        deployments = PluginDeployment.objects.select_related("plugin", "current_version", "previous_version")
        if slug:
            deployments = deployments.filter(plugin__slug=slug)
        for deployment in deployments:
            before = set(self.storage.list_versions(deployment.plugin.slug))
            retained = self.storage.retain_versions(
                deployment.plugin.slug,
                current=deployment.current_version.version,
                previous=deployment.previous_version.version if deployment.previous_version else "",
                keep=int(getattr(settings, "PLUGIN_KEEP_VERSIONS", 2)),
            )
            result["runtime_retained"][deployment.plugin.slug] = retained
            result["runtime_removed"].extend(
                f"{deployment.plugin.slug}/{version}" for version in sorted(before - set(retained))
            )

        preview_grace = int(getattr(settings, "PLUGIN_PREVIEW_GC_GRACE_SECONDS", 86400))
        preview_cutoff = now - preview_grace
        preview_roots = [self.storage.previews / slug] if slug else list(self.storage.previews.iterdir()) if self.storage.previews.is_dir() else []
        for project_directory in preview_roots:
            if not project_directory.is_dir():
                continue
            for version_directory in list(project_directory.iterdir()):
                if not version_directory.is_dir():
                    continue
                exists = PluginVersion.objects.filter(
                    plugin__slug=project_directory.name,
                    version=version_directory.name,
                ).exists()
                try:
                    expired = version_directory.stat().st_mtime <= preview_cutoff
                except FileNotFoundError:
                    continue
                if exists and not expired:
                    continue
                shutil.rmtree(version_directory, ignore_errors=True)
                result["preview_removed"].append(f"{project_directory.name}/{version_directory.name}")
            try:
                project_directory.rmdir()
            except OSError:
                pass

        gc_report = garbage_collect_package_blobs(root=self.storage.root)
        result["staging_removed"] += gc_report["staging_removed"]
        for key in ("package_blobs_removed", "orphan_files_removed", "missing_blob_files"):
            result[key] = gc_report[key]
        if gc_report["package_tombstones_restored"]:
            result["package_tombstones_restored"] = gc_report["package_tombstones_restored"]
        return result
