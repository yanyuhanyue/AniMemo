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

from .models import PluginData, PluginInstallation
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

    def _assert_growth_allowed(self):
        minimum = int(getattr(settings, "PLUGIN_MIN_FREE_DISK_MB", 2048)) * 1024 * 1024
        if shutil.disk_usage(self.storage.root).free < minimum:
            raise PluginInstallError("插件存储空间不足，无法安装或升级。")

    def install(self, uploaded_file, *, replace=False, actor=None):
        if not uploaded_file or not str(getattr(uploaded_file, "name", "")).lower().endswith(".ajplugin"):
            raise PluginInstallError("只支持上传 .ajplugin 插件包。")
        payload = uploaded_file.read()
        try:
            inspected = inspect_package(payload)
        except PluginPackageError as error:
            raise PluginInstallError(str(error)) from error
        manifest = inspected["manifest"]
        slug = manifest["slug"]
        version = manifest["version"]
        self.storage.ensure()

        with _PluginFilesystemLock(self.storage.root, slug):
            self._assert_growth_allowed()
            installation = PluginInstallation.objects.filter(slug=slug).first()
            if installation and installation.current_version == version:
                raise PluginInstallError("同版本插件已经安装；升级必须使用新的版本号。")
            if installation and not replace:
                raise PluginInstallError("插件已经安装；升级时必须明确确认替换当前版本。")
            if installation and installation.plugin_id != manifest["id"]:
                raise PluginInstallError("相同 slug 不能对应不同 plugin id。")
            if installation and installation.enabled and installation.healthy:
                try:
                    runtime_registry.ensure_current(slug)
                except RuntimeLoadError as error:
                    raise PluginInstallError(f"当前版本 Runtime 无法确认健康，升级已拒绝：{error}") from error

            package_target = self.storage.package_path(slug, version)
            runtime_target = self.storage.runtime / slug / version
            if package_target.exists() or runtime_target.exists():
                raise PluginInstallError("发现未登记的同版本文件；请先执行插件清理。")

            staging = self.storage.staging / f"{slug}-{version}-{uuid4().hex}"
            runtime_temp = runtime_target.with_name(f".{version}.staged-{uuid4().hex}")
            previous_candidate = None
            final_candidate = None
            activated = False
            package_written = False
            runtime_written = False
            old_version = installation.current_version if installation else ""
            was_enabled = bool(installation and installation.enabled)
            committed = False
            try:
                self._extract(payload, inspected, staging)
                shutil.copytree(staging, runtime_temp)
                health_candidate = runtime_registry.load_candidate(
                    runtime_temp,
                    expected_slug=slug,
                    expected_version=version,
                )
                health_candidate.dispose()

                runtime_target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(runtime_temp, runtime_target)
                runtime_written = True
                self.storage.store_package(slug, version, payload)
                package_written = True
                final_candidate = runtime_registry.load_candidate(
                    runtime_target,
                    expected_slug=slug,
                    expected_version=version,
                )

                with runtime_registry.plugin_lock(slug):
                    with transaction.atomic():
                        locked = PluginInstallation.objects.select_for_update().filter(slug=slug).first()
                        if locked is None:
                            locked = PluginInstallation(slug=slug, plugin_id=manifest["id"])
                        elif locked.current_version != old_version:
                            raise PluginInstallError("插件版本已被另一项操作修改。")
                        if locked.plugin_id != manifest["id"]:
                            raise PluginInstallError("plugin id 冲突。")
                        if was_enabled:
                            previous_candidate = runtime_registry.activate_candidate_locked(final_candidate)
                            activated = True
                        locked.previous_version = old_version
                        locked.current_version = version
                        locked.enabled = was_enabled
                        locked.healthy = True
                        locked.status = PluginInstallation.Status.ENABLED if was_enabled else PluginInstallation.Status.DEPLOYED
                        locked.last_error = ""
                        locked.config = locked.config if isinstance(locked.config, dict) else {}
                        locked.updated_by = actor
                        locked.disk_bytes = sum(path.stat().st_size for path in runtime_target.rglob("*") if path.is_file())
                        locked.save()
                committed = True
                if not activated and final_candidate is not None:
                    final_candidate.dispose()
                runtime_registry.finalize_previous(previous_candidate)
                self.storage.retain_versions(
                    slug,
                    current=version,
                    previous=old_version,
                    keep=int(getattr(settings, "PLUGIN_KEEP_VERSIONS", 2)),
                )
                return {
                    "slug": slug,
                    "version": version,
                    "replaced": bool(old_version),
                    "previous_version": old_version,
                    "restored_enabled": was_enabled,
                }
            except Exception as error:
                if activated:
                    with runtime_registry.plugin_lock(slug):
                        runtime_registry.restore_candidate_locked(slug, previous_candidate)
                elif final_candidate is not None:
                    final_candidate.dispose()
                if not committed and package_written:
                    package_target.unlink(missing_ok=True)
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
            installation = PluginInstallation.objects.filter(slug=slug).first()
            if installation is None or not installation.previous_version:
                raise PluginInstallError("没有可回滚的上一版本。")
            current_version = installation.current_version
            target_version = installation.previous_version
            if installation.enabled and installation.healthy:
                runtime_registry.ensure_current(slug)
            runtime_target = self.storage.runtime / slug / target_version
            if not runtime_target.is_dir():
                package_path = self.storage.package_path(slug, target_version)
                if not package_path.is_file():
                    raise PluginInstallError("上一版本 package/runtime 均不存在。")
                inspect_package(package_path.read_bytes())
                self.storage.rollback(slug, target_version)
            candidate = runtime_registry.load_candidate(
                runtime_target,
                expected_slug=slug,
                expected_version=target_version,
            )
            previous_candidate = None
            activated = False
            try:
                with runtime_registry.plugin_lock(slug):
                    with transaction.atomic():
                        locked = PluginInstallation.objects.select_for_update().get(slug=slug)
                        if locked.current_version != current_version or locked.previous_version != target_version:
                            raise PluginInstallError("插件版本已被另一项操作修改。")
                        if locked.enabled:
                            previous_candidate = runtime_registry.activate_candidate_locked(candidate)
                            activated = True
                        locked.current_version = target_version
                        locked.previous_version = current_version
                        locked.healthy = True
                        locked.last_error = ""
                        locked.status = PluginInstallation.Status.ENABLED if locked.enabled else PluginInstallation.Status.DEPLOYED
                        locked.updated_by = actor
                        locked.disk_bytes = sum(path.stat().st_size for path in runtime_target.rglob("*") if path.is_file())
                        locked.save()
                if not activated:
                    candidate.dispose()
                runtime_registry.finalize_previous(previous_candidate)
                self.storage.retain_versions(
                    slug,
                    current=target_version,
                    previous=current_version,
                    keep=int(getattr(settings, "PLUGIN_KEEP_VERSIONS", 2)),
                )
                return {"slug": slug, "version": target_version, "previous_version": current_version}
            except Exception:
                if activated:
                    with runtime_registry.plugin_lock(slug):
                        runtime_registry.restore_candidate_locked(slug, previous_candidate)
                else:
                    candidate.dispose()
                raise

    def set_enabled(self, slug, enabled, *, actor=None):
        self.storage.ensure()
        with _PluginFilesystemLock(self.storage.root, slug):
            installation = PluginInstallation.objects.filter(slug=slug).first()
            if installation is None:
                raise PluginInstallError("插件未安装。")
            candidate = None
            previous = None
            if enabled:
                candidate = runtime_registry.load_installed_candidate(slug, installation.current_version)
            try:
                with runtime_registry.plugin_lock(slug):
                    with transaction.atomic():
                        locked = PluginInstallation.objects.select_for_update().get(slug=slug)
                        if locked.current_version != installation.current_version:
                            raise PluginInstallError("插件版本已被另一项操作修改。")
                        if enabled:
                            previous = runtime_registry.activate_candidate_locked(candidate)
                            locked.healthy = True
                            locked.last_error = ""
                            locked.status = PluginInstallation.Status.ENABLED
                        else:
                            previous = runtime_registry._active.pop(slug, None)
                            if previous is not None:
                                previous.deactivate()
                            locked.status = PluginInstallation.Status.DEPLOYED
                        locked.enabled = bool(enabled)
                        locked.updated_by = actor
                        locked.save(update_fields=["enabled", "healthy", "last_error", "status", "updated_by", "updated_at"])
                if enabled:
                    runtime_registry.finalize_previous(previous)
                elif previous is not None:
                    previous.dispose()
                return locked
            except Exception:
                with runtime_registry.plugin_lock(slug):
                    if enabled and candidate is not None:
                        runtime_registry.restore_candidate_locked(slug, previous)
                    elif not enabled and previous is not None:
                        previous.activate()
                        runtime_registry._active[slug] = previous
                raise

    def uninstall(self, slug):
        self.storage.ensure()
        with _PluginFilesystemLock(self.storage.root, slug):
            with runtime_registry.plugin_lock(slug):
                previous = runtime_registry.active_candidate(slug)
                if previous is not None:
                    previous.deactivate()
                try:
                    with transaction.atomic():
                        installation = PluginInstallation.objects.select_for_update().get(slug=slug)
                        snapshot = {
                            "slug": installation.slug,
                            "plugin_id": installation.plugin_id,
                            "version": installation.current_version,
                        }
                        PluginData.objects.filter(plugin_slug=slug).delete()
                        installation.delete()
                except Exception:
                    if previous is not None:
                        previous.activate()
                    raise
                runtime_registry._active.pop(slug, None)
                if previous is not None:
                    previous.dispose()
            self.storage.delete_plugin(slug)
            return snapshot

    def cleanup(self, slug=None):
        self.storage.ensure()
        result = {"staging_removed": self.storage.cleanup_staging(), "plugins": {}}
        installations = PluginInstallation.objects.all()
        if slug:
            installations = installations.filter(slug=slug)
        for installation in installations:
            with _PluginFilesystemLock(self.storage.root, installation.slug):
                retained = self.storage.retain_versions(
                    installation.slug,
                    current=installation.current_version,
                    previous=installation.previous_version,
                    keep=int(getattr(settings, "PLUGIN_KEEP_VERSIONS", 2)),
                )
                result["plugins"][installation.slug] = retained
        return result
