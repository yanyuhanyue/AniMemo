import logging
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import ExpressionWrapper, F, PositiveSmallIntegerField, Q
from django.utils import timezone

from accounts.models import UserSecurityProfile

from .models import InstallationState


logger = logging.getLogger(__name__)


class FirstRunBootstrapError(RuntimeError):
    pass


class UnsafeSetupCodePath(FirstRunBootstrapError):
    pass


class SetupCompletionError(RuntimeError):
    def __init__(self, code, detail, status_code):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class ProvisionedSetupCode:
    path: Path
    reused: bool
    expires_at: object


def new_authentication_epoch():
    return secrets.token_hex(32)


def rotate_authentication_epoch():
    with transaction.atomic():
        installation = InstallationState.objects.select_for_update().get(pk=1)
        installation.authentication_epoch = new_authentication_epoch()
        installation.save(update_fields=["authentication_epoch", "updated_at"])
        UserSecurityProfile.objects.update(session_version=F("session_version") + 1)
        Session.objects.all().delete()
        return installation.authentication_epoch


def _record_failed_setup_attempt(snapshot):
    InstallationState.objects.filter(
        pk=1,
        status=InstallationState.Status.UNINITIALIZED,
        setup_code_hash=snapshot.setup_code_hash,
        setup_code_expires_at=snapshot.setup_code_expires_at,
        failed_attempts__lt=settings.FIRST_RUN_SETUP_MAX_ATTEMPTS,
    ).update(
        failed_attempts=ExpressionWrapper(
            F("failed_attempts") + 1,
            output_field=PositiveSmallIntegerField(),
        )
    )


def _is_reparse_point(metadata):
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(metadata, "st_file_attributes", 0) & marker)


def _assert_private_parent(path):
    try:
        metadata = path.parent.lstat()
    except FileNotFoundError as error:
        raise UnsafeSetupCodePath(f"Private setup directory is missing: {path.parent}") from error
    if not stat.S_ISDIR(metadata.st_mode) or path.parent.is_symlink() or _is_reparse_point(metadata):
        raise UnsafeSetupCodePath("Private setup directory must be a real directory, not a link or reparse point.")
    if os.name != "nt":
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise UnsafeSetupCodePath("Private setup directory permissions must not grant group or other access.")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise UnsafeSetupCodePath("Private setup directory must be owned by the API process user.")


def _assert_safe_existing_file(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink() or _is_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafeSetupCodePath("Setup code path must be a regular file and must not be a link.")
    if metadata.st_nlink != 1:
        raise UnsafeSetupCodePath("Setup code file must have exactly one hard link.")
    if os.name != "nt":
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise UnsafeSetupCodePath("Setup code file permissions must be 0600.")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise UnsafeSetupCodePath("Setup code file must be owned by the API process user.")
    return True


def read_private_setup_code(path):
    path = Path(path)
    _assert_private_parent(path)
    if not _assert_safe_existing_file(path):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafeSetupCodePath("Setup code file changed while it was being opened.")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            value = handle.read(4097)
        if len(value) > 4096:
            raise UnsafeSetupCodePath("Setup code file is unexpectedly large.")
        return value.strip()
    finally:
        os.close(descriptor)


def write_private_setup_code(path, value):
    path = Path(path)
    _assert_private_parent(path)
    _assert_safe_existing_file(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        payload = f"{value}\n".encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafeSetupCodePath("Temporary setup code file is not a private regular file.")
        os.close(descriptor)
        descriptor = None
        _assert_safe_existing_file(path)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _assert_safe_existing_file(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def delete_private_setup_code(path):
    path = Path(path)
    _assert_private_parent(path)
    if not _assert_safe_existing_file(path):
        return
    path.unlink()


def provision_first_run_setup():
    code_path = Path(settings.FIRST_RUN_SETUP_CODE_PATH)
    with transaction.atomic():
        installation = InstallationState.objects.select_for_update().get(pk=1)
        if installation.status == InstallationState.Status.INITIALIZED:
            if not installation.authentication_epoch:
                installation.authentication_epoch = new_authentication_epoch()
                installation.save(
                    update_fields=["authentication_epoch", "updated_at"]
                )
            delete_private_setup_code(code_path)
            return None
        if installation.status == InstallationState.Status.INITIALIZING:
            raise FirstRunBootstrapError("Installation is already initializing.")

        if installation.accepting_setup:
            existing = read_private_setup_code(code_path)
            if existing and check_password(existing, installation.setup_code_hash):
                return ProvisionedSetupCode(code_path, True, installation.setup_code_expires_at)

        code = secrets.token_urlsafe(32)
        issued_at = timezone.now()
        expires_at = issued_at + timedelta(seconds=settings.FIRST_RUN_SETUP_CODE_TTL_SECONDS)
        write_private_setup_code(code_path, code)
        installation.setup_code_hash = make_password(code)
        installation.setup_code_issued_at = issued_at
        installation.setup_code_expires_at = expires_at
        installation.failed_attempts = 0
        installation.save(update_fields=[
            "setup_code_hash",
            "setup_code_issued_at",
            "setup_code_expires_at",
            "failed_attempts",
            "updated_at",
        ])
        return ProvisionedSetupCode(code_path, False, expires_at)


def complete_first_run_setup(*, code, username, email, password, request):
    from django.contrib.auth import get_user_model
    from journal.models import AdminAuditLog, UserSettings
    from journal.network import client_ip

    User = get_user_model()
    snapshot = InstallationState.objects.only(
        "status",
        "setup_code_hash",
        "setup_code_expires_at",
    ).get(pk=1)
    now = timezone.now()
    if snapshot.status == InstallationState.Status.INITIALIZED:
        raise SetupCompletionError("installation_initialized", "安装已经完成。", 404)
    if snapshot.status == InstallationState.Status.INITIALIZING:
        raise SetupCompletionError(
            "installation_initializing",
            "安装初始化正在进行。",
            409,
        )
    if not snapshot.setup_code_hash or not snapshot.setup_code_expires_at:
        raise SetupCompletionError(
            "setup_code_unavailable",
            "初始化码不可用，请在服务器上重新生成。",
            409,
        )

    # The password hash check is intentionally outside the singleton row lock.
    # Public wrong-code attempts therefore cannot serialize an expensive PBKDF2
    # queue ahead of the legitimate initializer. The locked phase below binds
    # success to this exact hash/expiry snapshot before creating any user.
    code_matches = (
        snapshot.setup_code_expires_at > now
        and check_password(code, snapshot.setup_code_hash)
    )

    if not code_matches and snapshot.setup_code_expires_at > now:
        _record_failed_setup_attempt(snapshot)
        raise SetupCompletionError("invalid_setup_code", "初始化码无效。", 400)

    rejection = None
    created_user = None
    with transaction.atomic():
        installation = InstallationState.objects.select_for_update().get(pk=1)
        now = timezone.now()
        if installation.status == InstallationState.Status.INITIALIZED:
            rejection = SetupCompletionError("installation_initialized", "安装已经完成。", 404)
        elif installation.status == InstallationState.Status.INITIALIZING:
            rejection = SetupCompletionError("installation_initializing", "安装初始化正在进行。", 409)
        elif not installation.setup_code_hash or not installation.setup_code_expires_at:
            rejection = SetupCompletionError("setup_code_unavailable", "初始化码不可用，请在服务器上重新生成。", 409)
        elif (
            installation.setup_code_hash != snapshot.setup_code_hash
            or installation.setup_code_expires_at != snapshot.setup_code_expires_at
        ):
            rejection = SetupCompletionError(
                "setup_code_changed",
                "初始化码已经变化，请读取服务器上的当前初始化码后重试。",
                409,
            )
        elif installation.setup_code_expires_at <= now:
            delete_private_setup_code(settings.FIRST_RUN_SETUP_CODE_PATH)
            installation.setup_code_hash = ""
            installation.setup_code_issued_at = None
            installation.setup_code_expires_at = None
            installation.save(update_fields=[
                "setup_code_hash",
                "setup_code_issued_at",
                "setup_code_expires_at",
                "updated_at",
            ])
            rejection = SetupCompletionError("setup_code_expired", "初始化码已过期，请在服务器上重新生成。", 410)
        elif not code_matches:
            rejection = SetupCompletionError("invalid_setup_code", "初始化码无效。", 400)
        elif User.objects.filter(Q(username__iexact=username) | Q(email__iexact=email)).exists():
            rejection = SetupCompletionError(
                "admin_identity_unavailable",
                "该管理员用户名或邮箱已被使用，请改用其他值。",
                409,
            )
        else:
            installation.status = InstallationState.Status.INITIALIZING
            installation.save(update_fields=["status", "updated_at"])
            created_user = User.objects.create_superuser(
                username=username,
                email=User.objects.normalize_email(email),
                password=password,
            )
            UserSettings.objects.create(user=created_user, nickname=username)
            AdminAuditLog.objects.create(
                actor=created_user,
                action="installation.initialized",
                target_type="InstallationState",
                target_id="1",
                target_label="AniMemo first-run installation",
                after={"status": InstallationState.Status.INITIALIZED},
                metadata={"source": "browser-first-run"},
                ip_address=client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
            )
            delete_private_setup_code(settings.FIRST_RUN_SETUP_CODE_PATH)
            installation.status = InstallationState.Status.INITIALIZED
            installation.setup_code_hash = ""
            installation.setup_code_issued_at = None
            installation.setup_code_expires_at = None
            installation.failed_attempts = 0
            installation.authentication_epoch = new_authentication_epoch()
            installation.initialized_at = now
            installation.initialized_by = created_user
            installation.save(update_fields=[
                "status",
                "setup_code_hash",
                "setup_code_issued_at",
                "setup_code_expires_at",
                "failed_attempts",
                "authentication_epoch",
                "initialized_at",
                "initialized_by",
                "updated_at",
            ])
            user_id = created_user.pk

            def publish_created_user_event():
                from django.core.management import call_command
                from plugin_host.hooks import run_hook
                from plugin_host.sdk import UserHookContext

                try:
                    run_hook(
                        "user.after_created",
                        UserHookContext(user_id=user_id, source="first-run"),
                    )
                except Exception:
                    logger.exception("User creation plugin hook failed after first-run initialization")
                try:
                    call_command("sync_official_plugins", verbosity=0)
                except Exception:
                    logger.exception("Official plugin synchronization failed after first-run initialization")

            transaction.on_commit(publish_created_user_event)
    if rejection is not None:
        raise rejection
    return created_user
