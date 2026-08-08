from django.db import models


class PendingRegistration(models.Model):
    email = models.EmailField(unique=True)
    token_hash = models.CharField(max_length=128, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    completion_token_hash = models.CharField(max_length=128, blank=True, default="")
    completion_token_expires_at = models.DateTimeField(null=True, blank=True)
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent_digest = models.CharField(max_length=64, blank=True, default="")
    resend_count = models.PositiveIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["expires_at", "consumed_at"], name="accounts_pending_exp_idx"),
            models.Index(fields=["completion_token_expires_at"], name="accounts_pending_comp_exp_idx"),
        ]

    def __str__(self):
        return f"{self.email} · pending registration"
