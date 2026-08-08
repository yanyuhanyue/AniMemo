from django.conf import settings
from django.db import models


class UserSecurityProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="security_profile")
    session_version = models.PositiveIntegerField(default=1)
    email_verified = models.BooleanField(default=True)
    two_factor_enabled = models.BooleanField(default=False)
    totp_secret_encrypted = models.TextField(blank=True, default="", editable=False)
    pending_totp_secret_encrypted = models.TextField(blank=True, default="", editable=False)
    pending_totp_created_at = models.DateTimeField(blank=True, null=True)
    recovery_code_hashes = models.JSONField(blank=True, default=list, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    def set_totp_secret(self, value):
        from journal.secrets import encrypt_secret

        self.totp_secret_encrypted = encrypt_secret(value.strip())

    def get_totp_secret(self):
        from journal.secrets import decrypt_secret

        return decrypt_secret(self.totp_secret_encrypted) if self.totp_secret_encrypted else ""

    def set_pending_totp_secret(self, value):
        from journal.secrets import encrypt_secret

        self.pending_totp_secret_encrypted = encrypt_secret(value.strip())

    def get_pending_totp_secret(self):
        return self.__class__.get_pending_totp_secret_value(self.pending_totp_secret_encrypted)

    @staticmethod
    def get_pending_totp_secret_value(value):
        from journal.secrets import decrypt_secret

        return decrypt_secret(value) if value else ""

    def clear_pending_totp(self):
        self.pending_totp_secret_encrypted = ""
        self.pending_totp_created_at = None

    def __str__(self):
        return f"{self.user} · security v{self.session_version}"


class RevokedAccessToken(models.Model):
    jti = models.CharField(max_length=255, unique=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="revoked_access_tokens")
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revoked_at"]
        indexes = [models.Index(fields=["user", "expires_at"], name="accounts_rev_user_exp_idx")]

    def __str__(self):
        return f"{self.user_id} · {self.jti}"


class LoginEvent(models.Model):
    class EventType(models.TextChoices):
        LOGIN = "login", "登录"
        LOGIN_FAILED = "login_failed", "登录失败"
        TWO_FACTOR_FAILED = "two_factor_failed", "两步验证失败"
        FORCE_LOGOUT = "force_logout", "强制退出"
        TWO_FACTOR_ENABLED = "two_factor_enabled", "启用两步验证"
        TWO_FACTOR_DISABLED = "two_factor_disabled", "关闭两步验证"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name="login_events")
    account = models.CharField(max_length=254, blank=True, default="")
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    success = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "created_at"]), models.Index(fields=["success", "created_at"])]
