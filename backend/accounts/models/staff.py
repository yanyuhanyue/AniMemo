from django.conf import settings
from django.db import models


class StaffProfile(models.Model):
    class Role(models.TextChoices):
        UNASSIGNED = "unassigned", "未分配"
        REVIEWER = "reviewer", "内容审核员"
        USER_MANAGER = "user_manager", "用户管理员"
        OPERATOR = "operator", "系统运维员"
        ADMINISTRATOR = "administrator", "后台管理员"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profile")
    role = models.CharField(max_length=24, choices=Role.choices, default=Role.UNASSIGNED)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True, related_name="updated_staff_profiles")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} · {self.get_role_display()}"
