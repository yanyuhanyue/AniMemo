from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db import transaction

from .models import User


@admin.register(User)
class AccountsUserAdmin(UserAdmin):
    list_display = ("username", "email", "is_staff", "is_active", "date_joined")
    search_fields = ("username", "email")

    def delete_queryset(self, request, queryset):
        from journal.models import JournalEntry

        with transaction.atomic():
            # Evaluate before Collector.collect; select_for_update().delete()
            # alone discards the row-lock flag inside Django's collector.
            owners = list(queryset.select_for_update().order_by("pk"))
            list(JournalEntry.objects.filter(user_id__in=[owner.pk for owner in owners]).select_for_update().order_by("pk"))
            queryset.delete()

    def delete_model(self, request, obj):
        self.delete_queryset(request, User.objects.filter(pk=obj.pk))
