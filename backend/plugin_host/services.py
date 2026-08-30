from __future__ import annotations

import ast
import io
import os
import re
import shutil
from datetime import timedelta
from pathlib import Path, PurePosixPath
from uuid import uuid4
from zipfile import ZipFile

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .installer import PluginInstallError, PluginPackageInstaller
from .manifest import PLUGIN_ID_RE, SLUG_RE
from .models import (
    PluginData,
    PluginDeployment,
    PluginPackageBlob,
    PluginProject,
    PluginSubmission,
    PluginUploadAttempt,
    PluginVersion,
    UserPluginInstallation,
)
from .package import (
    LocalPluginPackageStorage,
    PluginPackageError,
    inspect_package,
    package_policy,
)
from .public_diagnostics import PLUGIN_SCAN_FAILED, stable_security_report


class PluginWorkflowError(ValueError):
    pass


DANGEROUS_IMPORTS = {
    "ctypes", "django", "multiprocessing", "os", "pathlib", "resource", "shutil", "signal", "socket", "subprocess", "sys",
}
DANGEROUS_CALLS = {"eval", "exec", "compile", "__import__", "os.system", "os.popen"}
GLOBAL_CSS_SELECTORS = {"*", ":root", "html", "body"}
GLOBAL_CSS_SELECTOR_RE = re.compile(r"(?:^|[\s>+~])(?:html|body|:root|\*)(?=$|[\s>+~.#:\[])")


def plugin_upload_policy():
    return package_policy()


def create_plugin_project(*, actor, plugin_id, slug, name, description):
    values = {
        "plugin_id": str(plugin_id or "").strip(),
        "slug": str(slug or "").strip(),
        "name": str(name or "").strip(),
        "description": str(description or "").strip(),
    }
    if not all(values.values()):
        raise PluginWorkflowError("plugin_id、slug、name、description 均不能为空。")
    if not PLUGIN_ID_RE.fullmatch(values["plugin_id"]):
        raise PluginWorkflowError("plugin_id 必须使用稳定命名空间，例如 com.example.my-plugin。")
    if not SLUG_RE.fullmatch(values["slug"]):
        raise PluginWorkflowError("slug 必须使用 kebab-case。")
    project = PluginProject(owner=actor, installation_mode=PluginProject.InstallationMode.USER, **values)
    try:
        project.full_clean()
        project.save()
    except (ValidationError, IntegrityError) as error:
        raise PluginWorkflowError("plugin_id 或 slug 已存在，或项目信息无效。") from error
    return project


def update_plugin_project(project, *, actor, name=None, description=None):
    with transaction.atomic():
        locked = PluginProject.objects.select_for_update().get(pk=project.pk)
        if locked.owner_id != actor.pk:
            raise PluginWorkflowError("只能修改自己的插件项目。")
        if locked.status == PluginProject.Status.ARCHIVED:
            raise PluginWorkflowError("已归档项目不能继续修改。")
        if name is not None:
            locked.name = str(name).strip()
        if description is not None:
            locked.description = str(description).strip()
        if not locked.name or not locked.description:
            raise PluginWorkflowError("名称和说明不能为空。")
        try:
            locked.full_clean(exclude=("owner",))
        except ValidationError as error:
            raise PluginWorkflowError("插件项目信息无效。") from error
        locked.save(update_fields=["name", "description", "updated_at"])
        return locked


def archive_or_delete_plugin_project(project, *, actor):
    with transaction.atomic():
        locked = PluginProject.objects.select_for_update().get(pk=project.pk)
        if locked.owner_id != actor.pk:
            raise PluginWorkflowError("只能删除自己的插件项目。")
        published = locked.versions.filter(published_at__isnull=False).exists()
        deployed = PluginDeployment.objects.filter(plugin=locked).exists()
        if published or deployed:
            locked.status = PluginProject.Status.ARCHIVED
            locked.save(update_fields=["status", "updated_at"])
            return "archived"
        slug = locked.slug
        locked.delete()
    LocalPluginPackageStorage(settings.PLUGIN_ROOT).delete_plugin(slug)
    return "deleted"


def static_security_scan(payload, inspected):
    manifest = inspected["manifest"]
    report = {
        "contains_backend": "backend" in set(manifest.get("runtimes") or []),
        "file_count": len(inspected["files"]),
        "package_size": len(payload),
        "uncompressed_size": sum(item["size"] for item in inspected["files"]),
        "uses_external_network": bool((manifest.get("dataPolicy") or {}).get("usesExternalNetwork")),
        "stores_personal_data": bool((manifest.get("dataPolicy") or {}).get("storesPersonalData")),
        "accepts_file_uploads": bool((manifest.get("dataPolicy") or {}).get("acceptsFileUploads")),
        "hooks": list(manifest.get("hooks") or []),
        "permissions": list(manifest.get("permissions") or []),
        "backend_imports": [],
        "dangerous_findings": [],
        "css_global_selectors": [],
    }
    with ZipFile(io.BytesIO(payload)) as archive:
        for item in inspected["files"]:
            path = item["path"]
            if path == "frontend/plugin.css":
                try:
                    css = archive.read(path).decode("utf-8")
                except UnicodeDecodeError:
                    report["dangerous_findings"].append(PLUGIN_SCAN_FAILED)
                else:
                    for group in re.findall(r"(?:^|})\s*([^@}{]+)\{", css, flags=re.MULTILINE):
                        for selector in group.split(","):
                            normalized = selector.strip().casefold()
                            if normalized in GLOBAL_CSS_SELECTORS or GLOBAL_CSS_SELECTOR_RE.search(normalized):
                                report["css_global_selectors"].append(normalized)
            if not path.startswith("backend/") or not path.endswith(".py"):
                continue
            try:
                tree = ast.parse(archive.read(path).decode("utf-8"), filename=path)
            except (SyntaxError, UnicodeDecodeError):
                report["dangerous_findings"].append(PLUGIN_SCAN_FAILED)
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    report["backend_imports"].extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    report["backend_imports"].append(node.module)
                elif isinstance(node, ast.Call):
                    name = ""
                    if isinstance(node.func, ast.Name):
                        name = node.func.id
                    elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                        name = f"{node.func.value.id}.{node.func.attr}"
                    if name in DANGEROUS_CALLS:
                        report["dangerous_findings"].append(f"{path}:{getattr(node, 'lineno', 0)} uses {name}")
    report["backend_imports"] = sorted(set(report["backend_imports"]))
    report["css_global_selectors"] = sorted(set(report["css_global_selectors"]))
    for name in report["backend_imports"]:
        if name.split(".", 1)[0] in DANGEROUS_IMPORTS:
            report["dangerous_findings"].append(f"dangerous import: {name}")
    return stable_security_report(report)


def store_package_blob(payload, *, root=None, minimum_free_bytes=0):
    raw = payload.read() if hasattr(payload, "read") else bytes(payload)
    inspected = inspect_package(raw)
    storage = LocalPluginPackageStorage(root or settings.PLUGIN_ROOT)
    path = storage.store_package(
        raw,
        sha256=inspected["sha256"],
        minimum_free_bytes=minimum_free_bytes,
    )
    relative = path.relative_to(storage.root).as_posix()
    try:
        with transaction.atomic():
            blob, created = PluginPackageBlob.objects.get_or_create(
                sha256=inspected["sha256"],
                defaults={"size_bytes": len(raw), "storage_path": relative},
            )
    except IntegrityError:
        blob = PluginPackageBlob.objects.get(sha256=inspected["sha256"])
        created = False
    if blob.size_bytes != len(raw) or blob.storage_path != relative:
        raise PluginWorkflowError("CAS 数据库记录与物理文件不一致。")
    return blob, inspected, raw, created


def _record_upload_attempt(actor, size_bytes):
    if actor.is_superuser:
        return None, False
    now = timezone.now()
    recent_limit = int(getattr(settings, "PLUGIN_UPLOADS_PER_HOUR", 12))
    user_model = type(actor)
    with transaction.atomic():
        user_model.objects.select_for_update().get(pk=actor.pk)
        recent_count = PluginUploadAttempt.objects.filter(
            user_id=actor.pk,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        limited = recent_count >= recent_limit
        attempt = PluginUploadAttempt.objects.create(
            user_id=actor.pk,
            size_bytes=max(0, int(size_bytes)),
            accepted=False,
            outcome="rate_limited" if limited else "received",
        )
    return attempt, limited


def _finish_upload_attempt(attempt, *, accepted=False, outcome):
    if attempt is None:
        return
    PluginUploadAttempt.objects.filter(pk=attempt.pk).update(accepted=bool(accepted), outcome=str(outcome)[:32])


def upload_plugin_version(project, uploaded_file, *, actor):
    if not uploaded_file or not str(getattr(uploaded_file, "name", "")).lower().endswith(".ajplugin"):
        raise PluginWorkflowError("只支持上传 .ajplugin 插件包。")
    policy = package_policy()
    declared_size = getattr(uploaded_file, "size", None)
    try:
        declared_size = max(0, int(declared_size)) if declared_size is not None else None
    except (TypeError, ValueError):
        declared_size = None
    attempt, rate_limited = _record_upload_attempt(actor, declared_size or 0)
    try:
        # Reject throttled and obviously oversized uploads before touching the
        # request body. UploadedFile.size is populated by Django's multipart
        # parser; the payload length remains a second-line guard for callers
        # that do not provide a reliable declared size.
        if rate_limited:
            raise PluginWorkflowError("上传过于频繁，请稍后再试。")
        if declared_size is not None and declared_size > policy["max_package_bytes"]:
            raise PluginPackageError("插件包超过大小限制")
        raw = uploaded_file.read()
        if len(raw) > policy["max_package_bytes"]:
            raise PluginPackageError("插件包超过大小限制")
        inspected = inspect_package(raw)
        manifest = inspected["manifest"]
        with transaction.atomic():
            locked_project = PluginProject.objects.select_for_update().get(pk=project.pk)
            if locked_project.owner_id != actor.pk and not actor.is_superuser:
                raise PluginWorkflowError("只能向自己的插件项目上传版本。")
            if locked_project.status != PluginProject.Status.ACTIVE:
                raise PluginWorkflowError("只有活跃插件项目可以上传新版本。")
            if manifest["id"] != locked_project.plugin_id or manifest["slug"] != locked_project.slug:
                raise PluginWorkflowError("Manifest id/slug 与插件项目不一致。")
            if manifest["installationMode"] != locked_project.installation_mode:
                raise PluginWorkflowError("Manifest installationMode 与插件项目不一致。")
            existing = PluginVersion.objects.filter(plugin=locked_project, version=manifest["version"]).first()
            if existing:
                if existing.package_blob.sha256 != inspected["sha256"]:
                    raise PluginWorkflowError("同一插件版本的 Package SHA-256 不可改变；请提升版本号。")
                report = static_security_scan(raw, inspected)
                _finish_upload_attempt(attempt, accepted=True, outcome="duplicate")
                return existing, report, False
            if not actor.is_superuser:
                draft_limit = int(getattr(settings, "PLUGIN_DRAFT_LIMIT", 20))
                if PluginVersion.objects.filter(
                    plugin__owner=actor,
                    review_status=PluginVersion.ReviewStatus.DRAFT,
                ).count() >= draft_limit:
                    raise PluginWorkflowError("未审核草稿数量已达上限。")
            # Identity, ownership and draft/version constraints are cheap and
            # must pass before the AST/CSS security scan does any work.
            report = static_security_scan(raw, inspected)
            minimum_free = int(getattr(settings, "PLUGIN_MIN_FREE_DISK_MB", 2048)) * 1024 * 1024
            blob, _, _, _ = store_package_blob(raw, minimum_free_bytes=minimum_free)
            locked_blob = PluginPackageBlob.objects.select_for_update().get(pk=blob.pk)
            version = PluginVersion.objects.create(
                plugin=locked_project,
                version=manifest["version"],
                package_blob=locked_blob,
                manifest_snapshot=manifest,
                runtime_types=manifest.get("runtimes") or [],
                created_by=actor,
            )
    except (PluginWorkflowError, PluginPackageError):
        if attempt is not None and attempt.outcome != "rate_limited":
            _finish_upload_attempt(attempt, outcome="rejected")
        raise
    except IntegrityError as error:
        _finish_upload_attempt(attempt, outcome="conflict")
        raise PluginWorkflowError("同一插件版本的 Package SHA-256 不可改变；请提升版本号。") from error
    _finish_upload_attempt(attempt, accepted=True, outcome="accepted")
    return version, report, True


def create_frontend_preview(plugin_version, *, actor):
    plugin_version = PluginVersion.objects.select_related("plugin", "package_blob").get(pk=plugin_version.pk)
    if plugin_version.plugin.owner_id != actor.pk:
        raise PluginWorkflowError("只有插件作者可以创建草稿预览。")
    if plugin_version.review_status not in {PluginVersion.ReviewStatus.DRAFT, PluginVersion.ReviewStatus.REJECTED}:
        raise PluginWorkflowError("只有草稿或被拒绝的版本可以创建私人预览。")
    if set(plugin_version.runtime_types or []) != {"frontend"}:
        raise PluginWorkflowError("只有 frontend-only 草稿允许私人预览。")
    storage = LocalPluginPackageStorage(settings.PLUGIN_ROOT)
    source = storage.package_path(plugin_version.package_blob.sha256)
    target = storage.previews / plugin_version.plugin.slug / plugin_version.version
    temporary = target.with_name(f".{target.name}.preview")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True, exist_ok=False)
    with ZipFile(source) as archive:
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if info.is_dir() or not (path == PurePosixPath("manifest.json") or path.parts[0] == "frontend"):
                continue
            destination = temporary.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    temporary.replace(target)
    return target


def _payload_for_version(plugin_version):
    storage = LocalPluginPackageStorage(settings.PLUGIN_ROOT)
    path = storage.package_path(plugin_version.package_blob.sha256)
    if not path.is_file():
        raise PluginWorkflowError("插件 Package 文件不存在。")
    payload = path.read_bytes()
    inspected = inspect_package(payload)
    if inspected["sha256"] != plugin_version.package_blob.sha256 or inspected["manifest"] != plugin_version.manifest_snapshot:
        raise PluginWorkflowError("插件不可变 Package 校验失败。")
    return payload, inspected


def submit_version(plugin_version, *, actor):
    payload, inspected = _payload_for_version(
        PluginVersion.objects.select_related("package_blob").get(pk=plugin_version.pk)
    )
    security_report = static_security_scan(payload, inspected)
    with transaction.atomic():
        version = PluginVersion.objects.select_for_update().select_related("plugin").get(pk=plugin_version.pk)
        if version.plugin.owner_id != actor.pk or version.review_status not in {
            PluginVersion.ReviewStatus.DRAFT, PluginVersion.ReviewStatus.REJECTED,
        }:
            raise PluginWorkflowError("该版本当前不能提交审核。")
        if version.submissions.filter(status=PluginSubmission.Status.SUBMITTED).exists():
            raise PluginWorkflowError("该版本已经存在待审核提交。")
        version.review_status = PluginVersion.ReviewStatus.SUBMITTED
        version.save(update_fields=["review_status"])
        try:
            return PluginSubmission.objects.create(
                plugin_version=version,
                submitter=actor,
                security_report=security_report,
            )
        except IntegrityError as error:
            raise PluginWorkflowError("该版本已经存在待审核提交。") from error


def withdraw_submission(submission, *, actor):
    with transaction.atomic():
        locked = PluginSubmission.objects.select_for_update().select_related("plugin_version__plugin").get(pk=submission.pk)
        if locked.plugin_version.plugin.owner_id != actor.pk or locked.status != PluginSubmission.Status.SUBMITTED:
            raise PluginWorkflowError("该审核提交当前不能撤回。")
        locked.status = PluginSubmission.Status.WITHDRAWN
        locked.review_note = "作者撤回审核。"
        locked.save(update_fields=["status", "review_note"])
        version = locked.plugin_version
        version.review_status = PluginVersion.ReviewStatus.DRAFT
        version.save(update_fields=["review_status"])
        return locked


def review_submission(submission, *, actor, approve, note=""):
    with transaction.atomic():
        locked = PluginSubmission.objects.select_for_update().select_related("plugin_version").get(pk=submission.pk)
        if locked.status != PluginSubmission.Status.SUBMITTED:
            raise PluginWorkflowError("该提交已处理。")
        locked.status = PluginSubmission.Status.APPROVED if approve else PluginSubmission.Status.REJECTED
        locked.reviewer = actor
        locked.reviewed_at = timezone.now()
        locked.review_note = note
        locked.save()
        version = locked.plugin_version
        version.review_status = PluginVersion.ReviewStatus.APPROVED if approve else PluginVersion.ReviewStatus.REJECTED
        version.save(update_fields=["review_status"])
    return locked


def install_for_user(plugin, *, user):
    if plugin.installation_mode != PluginProject.InstallationMode.USER or plugin.status != PluginProject.Status.ACTIVE:
        raise PluginWorkflowError("该插件不允许用户安装。")
    deployment = PluginDeployment.objects.filter(
        plugin=plugin,
        enabled=True,
        healthy=True,
        current_version__published_at__isnull=False,
        current_version__revoked_at__isnull=True,
    ).first()
    if deployment is None:
        raise PluginWorkflowError("该插件当前没有可安装的已发布版本。")
    try:
        with transaction.atomic():
            installation, created = UserPluginInstallation.objects.get_or_create(
                user=user,
                plugin=plugin,
                defaults={"enabled": True},
            )
            if not created and not installation.enabled:
                installation.enabled = True
                installation.save(update_fields=["enabled", "updated_at"])
    except IntegrityError:
        installation = UserPluginInstallation.objects.get(user=user, plugin=plugin)
        created = False
    return installation, created


def uninstall_for_user(plugin, *, user, delete_data=False):
    deleted = UserPluginInstallation.objects.filter(user=user, plugin=plugin).delete()[0]
    if delete_data:
        PluginData.objects.filter(plugin=plugin, user=user).delete()
    return deleted


def unpublish_version(plugin_version):
    plugin_version.published_at = None
    plugin_version.save(update_fields=["published_at"])
    return plugin_version


def publish_version(plugin_version, *, actor):
    return PluginPackageInstaller().publish(plugin_version, actor=actor)


def _reconcile_gc_tombstones(storage):
    removed = 0
    restored = []
    gc_directory = storage.staging / "gc"
    if not gc_directory.is_dir():
        return removed, restored
    for tombstone in list(gc_directory.glob("*.tombstone")):
        digest = tombstone.name.split(".", 1)[0].lower()
        try:
            canonical = storage.package_path(digest)
        except PluginPackageError:
            tombstone.unlink(missing_ok=True)
            removed += 1
            continue
        with storage.package_lock(digest):
            blob_exists = PluginPackageBlob.objects.filter(sha256=digest).exists()
            if blob_exists and not canonical.exists():
                canonical.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tombstone, canonical)
                restored.append(digest)
            else:
                tombstone.unlink(missing_ok=True)
                removed += 1
    return removed, restored


def garbage_collect_package_blobs(*, root=None):
    storage = LocalPluginPackageStorage(root or settings.PLUGIN_ROOT)
    storage.ensure()
    report = {
        "package_blobs_removed": [],
        "orphan_files_removed": [],
        "missing_blob_files": [],
        "package_tombstones_restored": [],
        "staging_removed": 0,
    }
    cutoff = timezone.now() - timedelta(seconds=int(getattr(settings, "PLUGIN_PACKAGE_GC_GRACE_SECONDS", 86400)))
    staging_removed, restored = _reconcile_gc_tombstones(storage)
    report["staging_removed"] += staging_removed
    report["package_tombstones_restored"].extend(restored)

    candidate_hashes = list(
        PluginPackageBlob.objects.filter(versions__isnull=True, created_at__lte=cutoff).values_list("sha256", flat=True)
    )
    for digest in candidate_hashes:
        canonical = storage.package_path(digest)
        tombstone = None
        committed = False
        with storage.package_lock(digest):
            try:
                with transaction.atomic():
                    blob = PluginPackageBlob.objects.select_for_update().filter(sha256=digest).first()
                    if blob is None or blob.created_at > cutoff or blob.versions.exists():
                        continue
                    if not canonical.is_file():
                        report["missing_blob_files"].append(digest)
                        continue
                    gc_directory = storage.staging / "gc"
                    gc_directory.mkdir(parents=True, exist_ok=True)
                    tombstone = gc_directory / f"{digest}.{uuid4().hex}.tombstone"
                    os.replace(canonical, tombstone)
                    blob.delete()
                committed = True
                tombstone.unlink(missing_ok=True)
                report["package_blobs_removed"].append(digest)
            except Exception:
                if not committed and tombstone is not None and tombstone.exists() and not canonical.exists():
                    canonical.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tombstone, canonical)
                raise

    for blob in PluginPackageBlob.objects.only("sha256"):
        digest = blob.sha256
        with storage.package_lock(digest):
            if PluginPackageBlob.objects.filter(sha256=digest).exists() and not storage.package_path(digest).is_file():
                report["missing_blob_files"].append(digest)

    package_root = storage.packages / "sha256"
    if package_root.is_dir():
        for path in list(package_root.rglob("*.ajplugin")):
            digest = path.stem.lower()
            try:
                canonical = storage.package_path(digest)
            except PluginPackageError:
                continue
            if path != canonical:
                continue
            try:
                modified_at = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if modified_at > cutoff.timestamp():
                continue
            with storage.package_lock(digest):
                if PluginPackageBlob.objects.filter(sha256=digest).exists() or not path.is_file():
                    continue
                path.unlink()
                report["orphan_files_removed"].append(path.relative_to(storage.root).as_posix())

    report["missing_blob_files"] = sorted(set(report["missing_blob_files"]))
    return report
