from __future__ import annotations

import hashlib
import os
import shutil
import time
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import ZipFile

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .filesystem_security import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    PluginFilesystemSecurityError,
    close_and_unlink_created_file,
    contained_path,
    created_file_identity,
    ensure_directory,
    ensure_plugin_layout,
    remove_secure_tree,
    require_created_file_identity,
    secure_file,
    secure_tree,
    write_descriptor_all,
    write_secure_bytes,
)
from .manifest import ManifestError, parse_version, validate_slug, validate_version
from .models import PluginDeployment, PluginVersion
from .package import (
    LocalPluginPackageStorage,
    PluginPackageError,
    inspect_package,
    read_validated_package_member,
    validated_package_members,
)
from .runtime import RuntimeLoadError, runtime_registry


class PluginInstallError(PluginPackageError):
    pass


class _PluginFilesystemLock:
    def __init__(self, root, slug, timeout=10):
        try:
            slug = validate_slug(slug)
        except ManifestError as error:
            raise PluginInstallError("插件生命周期锁 slug 无效。") from error
        storage_root = Path(root)
        self.root = contained_path(storage_root, storage_root / ".locks")
        self.path = contained_path(
            self.root,
            self.root / f"{slug}.lock",
        )
        self.timeout = timeout
        self.fd = None
        self.token = f"{os.getpid()}:{uuid4().hex}"
        self.identity = None
        self.payload = f"{self.token}\n".encode("ascii")

    def __enter__(self):
        try:
            ensure_directory(self.root, self.path.parent, mode=PRIVATE_DIRECTORY_MODE)
        except PluginFilesystemSecurityError as error:
            raise PluginInstallError("插件生命周期锁目录不安全。") from error
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                self.fd = os.open(self.path, flags, PRIVATE_FILE_MODE)
            except FileExistsError:
                try:
                    secure_file(self.path.parent, self.path, mode=PRIVATE_FILE_MODE)
                    stale = time.time() - self.path.stat().st_mtime > 300
                except FileNotFoundError:
                    continue
                except PluginFilesystemSecurityError as error:
                    raise PluginInstallError("插件生命周期锁文件不安全。") from error
                if stale:
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise PluginInstallError("同一插件正在执行另一项生命周期操作。")
                time.sleep(0.05)
                continue
            except OSError as error:
                raise PluginInstallError("插件生命周期锁文件无法创建。") from error

            try:
                self.identity = created_file_identity(self.fd)
                if hasattr(os, "fchmod") and os.name != "nt":
                    os.fchmod(self.fd, PRIVATE_FILE_MODE)
                write_descriptor_all(self.fd, self.payload)
                os.fsync(self.fd)
                secure_file(self.path.parent, self.path, mode=PRIVATE_FILE_MODE)
                require_created_file_identity(
                    self.root,
                    self.path,
                    self.identity,
                )
                return self
            except BaseException as error:
                close_and_unlink_created_file(
                    self.root,
                    self.path,
                    self.fd,
                    self.identity,
                )
                self.fd = None
                self.identity = None
                if isinstance(error, (OSError, PluginFilesystemSecurityError)):
                    raise PluginInstallError("插件生命周期锁文件无法安全初始化。") from error
                raise

    def __exit__(self, exc_type, exc, traceback):
        if self.fd is None:
            return
        close_and_unlink_created_file(
            self.root,
            self.path,
            self.fd,
            self.identity,
            expected_payload=self.payload,
        )
        self.fd = None
        self.identity = None


class PluginPackageInstaller:
    """Deploys only reviewed immutable PluginVersion rows.

    Developer uploads never call this class. Loading Python is intentionally
    confined to publish/rollback, both trusted administrator operations.
    """

    def __init__(self, root=None):
        self.storage = LocalPluginPackageStorage(root or settings.PLUGIN_ROOT)

    @staticmethod
    def _validated_slug(slug):
        try:
            return validate_slug(slug)
        except ManifestError as error:
            raise PluginInstallError("插件 slug 无效。") from error

    @staticmethod
    def _validated_version(version):
        try:
            return validate_version(version)
        except ManifestError as error:
            raise PluginInstallError("插件版本无效。") from error

    @classmethod
    def _validate_deployment_identity(cls, deployment):
        cls._validated_slug(deployment.plugin.slug)
        cls._validated_version(deployment.current_version.version)
        if deployment.previous_version is not None:
            cls._validated_version(deployment.previous_version.version)
        if deployment.rollback_floor:
            cls._validated_version(deployment.rollback_floor)

    @classmethod
    def _manifest_rollback_floor(cls, plugin_version):
        snapshot = plugin_version.manifest_snapshot
        if not isinstance(snapshot, dict):
            raise PluginInstallError("不可变版本的 Manifest 快照无效。")
        compatibility = snapshot.get("dataCompatibility")
        if compatibility is None:
            return ""
        if not isinstance(compatibility, dict):
            raise PluginInstallError("不可变版本的数据兼容声明无效。")
        floor = compatibility.get("rollbackFloor", "")
        if floor == "":
            return ""
        return cls._validated_version(floor)

    def _extract(self, payload, inspected, destination):
        destination = ensure_directory(
            self.storage.staging,
            destination,
            mode=PRIVATE_DIRECTORY_MODE,
        )
        expected = {entry["path"]: entry for entry in inspected["files"]}
        extracted = set()
        try:
            with ZipFile(BytesIO(payload)) as archive:
                members, budget = validated_package_members(archive)
                for info, relative in members:
                    content = read_validated_package_member(
                        archive,
                        info,
                        budget,
                    )
                    entry = expected.get(relative.as_posix())
                    if entry is None:
                        if relative == PurePosixPath("package-index.json"):
                            continue
                        raise PluginInstallError(
                            "插件包成员与已验证索引不一致。"
                        )
                    if (
                        len(content) != entry["size"]
                        or hashlib.sha256(content).hexdigest()
                        != entry["sha256"]
                    ):
                        raise PluginInstallError("插件包成员完整性校验失败。")
                    extracted.add(relative.as_posix())
                    target = destination.joinpath(*relative.parts)
                    write_secure_bytes(
                        destination,
                        target,
                        content,
                        directory_mode=PRIVATE_DIRECTORY_MODE,
                        file_mode=PRIVATE_FILE_MODE,
                    )
        except PluginInstallError:
            raise
        except PluginPackageError as error:
            raise PluginInstallError(str(error)) from error
        if extracted != set(expected):
            raise PluginInstallError("插件包成员与已验证索引不一致。")
        secure_tree(
            self.storage.staging,
            destination,
            directory_mode=PRIVATE_DIRECTORY_MODE,
            file_mode=PRIVATE_FILE_MODE,
        )

    def _assert_growth_allowed(self, expanded_bytes, *, peak_multiplier=2):
        ensure_plugin_layout(self.storage)
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
        secure_file(self.storage.packages, path, mode=PRIVATE_FILE_MODE)
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

        slug = self._validated_slug(plugin.slug)
        version = self._validated_version(plugin_version.version)
        self._manifest_rollback_floor(plugin_version)
        preflight_deployment = PluginDeployment.objects.select_related(
            "plugin", "current_version", "previous_version"
        ).filter(plugin=plugin).first()
        if preflight_deployment is not None:
            self._validate_deployment_identity(preflight_deployment)
            if preflight_deployment.current_version_id == plugin_version.pk:
                raise PluginInstallError("该版本已经是当前部署版本。")

        ensure_plugin_layout(self.storage)
        with _PluginFilesystemLock(self.storage.root, slug):
            deployment = PluginDeployment.objects.select_related(
                "plugin", "current_version", "previous_version"
            ).filter(plugin=plugin).first()
            if deployment is not None:
                self._validate_deployment_identity(deployment)
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
                key=parse_version,
                default="",
            )
            if effective_floor and parse_version(version) < parse_version(effective_floor):
                raise PluginInstallError(f"版本 {version} 低于数据兼容下限 {effective_floor}，不能发布。")
            expanded_bytes = sum(item["size"] for item in inspected["files"])
            # Publish keeps both the extracted staging tree and a temporary
            # runtime tree before the atomic replacement, so reserve 2x the
            # expanded package size in addition to the configured free floor.
            self._assert_growth_allowed(expanded_bytes, peak_multiplier=2)
            runtime_target = contained_path(
                self.storage.root,
                self.storage.runtime / slug / version,
            )
            staging = contained_path(
                self.storage.root,
                self.storage.staging / f"{slug}-{version}-{uuid4().hex}",
            )
            runtime_temp = contained_path(
                self.storage.root,
                runtime_target.with_name(f".{version}.staged-{uuid4().hex}"),
            )
            previous_candidate = None
            final_candidate = None
            activated = False
            runtime_written = False
            committed = False
            was_enabled = True if deployment is None else deployment.enabled
            try:
                if runtime_target.exists():
                    remove_secure_tree(self.storage.runtime, runtime_target)
                self._extract(payload, inspected, staging)
                ensure_directory(
                    self.storage.runtime,
                    runtime_target.parent,
                    mode=RUNTIME_DIRECTORY_MODE,
                )
                shutil.copytree(staging, runtime_temp)
                secure_tree(
                    self.storage.runtime,
                    runtime_temp,
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
                candidate = runtime_registry.load_candidate(runtime_temp, expected_slug=slug, expected_version=version)
                candidate.dispose()

                os.replace(runtime_temp, runtime_target)
                secure_tree(
                    self.storage.runtime,
                    runtime_target,
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
                runtime_written = True
                final_candidate = runtime_registry.load_candidate(runtime_target, expected_slug=slug, expected_version=version)

                with runtime_registry.plugin_lock(slug), transaction.atomic():
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
                    remove_secure_tree(self.storage.runtime, runtime_target)
                if isinstance(error, (PluginInstallError, RuntimeLoadError, PluginPackageError, PluginFilesystemSecurityError)):
                    raise PluginInstallError(str(error)) from error
                raise
            finally:
                remove_secure_tree(self.storage.runtime, runtime_temp)
                remove_secure_tree(self.storage.staging, staging)

    def rollback(self, slug, *, actor=None):
        slug = self._validated_slug(slug)
        deployment = PluginDeployment.objects.select_related(
            "plugin", "current_version", "previous_version", "previous_version__package_blob"
        ).filter(plugin__slug=slug).first()
        if deployment is None or deployment.previous_version is None:
            raise PluginInstallError("没有可回滚的上一版本。")
        self._validate_deployment_identity(deployment)
        if deployment.previous_version.revoked_at:
            raise PluginInstallError("上一版本已撤销，不能回滚。")
        if (
            deployment.rollback_floor
            and parse_version(deployment.previous_version.version)
            < parse_version(deployment.rollback_floor)
        ):
            raise PluginInstallError(
                f"目标版本 {deployment.previous_version.version} 低于数据兼容下限 "
                f"{deployment.rollback_floor}，不能回滚。"
            )

        ensure_plugin_layout(self.storage)
        with _PluginFilesystemLock(self.storage.root, slug):
            deployment = PluginDeployment.objects.select_related(
                "plugin", "current_version", "previous_version", "previous_version__package_blob"
            ).filter(plugin__slug=slug).first()
            if deployment is None or deployment.previous_version is None:
                raise PluginInstallError("没有可回滚的上一版本。")
            self._validate_deployment_identity(deployment)
            current_version = deployment.current_version
            target_version = deployment.previous_version
            if target_version.revoked_at:
                raise PluginInstallError("上一版本已撤销，不能回滚。")
            if (
                deployment.rollback_floor
                and parse_version(target_version.version)
                < parse_version(deployment.rollback_floor)
            ):
                raise PluginInstallError(
                    f"目标版本 {target_version.version} 低于数据兼容下限 {deployment.rollback_floor}，不能回滚。"
                )
            if deployment.enabled and deployment.healthy:
                runtime_registry.ensure_current(slug)
            runtime_target = contained_path(
                self.storage.root,
                self.storage.runtime / slug / target_version.version,
            )
            if not runtime_target.is_dir():
                _, inspected = self._payload_for(target_version)
                expanded_bytes = sum(item["size"] for item in inspected["files"])
                self._assert_growth_allowed(expanded_bytes, peak_multiplier=2)
                self.storage.rollback(slug, target_version.version, target_version.package_blob.sha256)
                secure_tree(
                    self.storage.runtime,
                    runtime_target,
                    directory_mode=RUNTIME_DIRECTORY_MODE,
                    file_mode=RUNTIME_FILE_MODE,
                )
            candidate = runtime_registry.load_candidate(runtime_target, expected_slug=slug, expected_version=target_version.version)
            previous_candidate = None
            activated = False
            try:
                with runtime_registry.plugin_lock(slug), transaction.atomic():
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
        slug = self._validated_slug(slug)
        deployment = PluginDeployment.objects.select_related("plugin", "current_version").filter(plugin__slug=slug).first()
        if deployment is None:
            raise PluginInstallError("插件尚未部署。")
        self._validate_deployment_identity(deployment)
        ensure_plugin_layout(self.storage)
        candidate = runtime_registry.load_installed_candidate(slug, deployment.current_version.version) if enabled else None
        previous = None
        with _PluginFilesystemLock(self.storage.root, slug), runtime_registry.plugin_lock(slug):
            try:
                with transaction.atomic():
                    # previous_version is nullable, so PostgreSQL must not try
                    # to lock that select_related outer-join target.
                    locked = PluginDeployment.objects.select_for_update(
                        of=("self",)
                    ).select_related(
                        "plugin", "current_version", "previous_version"
                    ).get(pk=deployment.pk)
                    self._validate_deployment_identity(locked)
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
                deployment.last_error = ""
                deployment.updated_by = actor
                deployment.save()
        if deployment:
            runtime_registry.unload(deployment.plugin.slug)
        return locked_version

    def cleanup(self, slug=None):
        if slug is not None:
            slug = self._validated_slug(slug)
        from .services import garbage_collect_package_blobs

        deployments = PluginDeployment.objects.select_related(
            "plugin", "current_version", "previous_version"
        )
        if slug:
            deployments = deployments.filter(plugin__slug=slug)
        deployments = list(deployments)
        for deployment in deployments:
            self._validate_deployment_identity(deployment)

        ensure_plugin_layout(self.storage)
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
            ensure_directory(
                self.storage.previews,
                project_directory,
                mode=PRIVATE_DIRECTORY_MODE,
            )
            for version_directory in list(project_directory.iterdir()):
                if not version_directory.is_dir():
                    continue
                ensure_directory(
                    project_directory,
                    version_directory,
                    mode=PRIVATE_DIRECTORY_MODE,
                )
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
                remove_secure_tree(project_directory, version_directory)
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
