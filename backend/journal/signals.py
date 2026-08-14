from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from rest_framework.exceptions import ValidationError as DRFValidationError

from .image_security import sanitize_uploaded_image, schedule_file_delete
from site_config.models import SiteSettings
from accounts.models import StaffProfile
from .models import Column, JournalEntry, UserSettings

@receiver(pre_save, sender=get_user_model())
def normalize_user_email(sender, instance, raw=False, **_kwargs):
    if raw:
        return
    instance.email = str(instance.email or "").strip().lower()
    instance.username = str(instance.username or "").strip()


@receiver(post_save, sender=get_user_model())
def remove_staff_profile_when_account_is_demoted(sender, instance, raw=False, **_kwargs):
    if raw or not instance.pk or instance.is_staff:
        return
    StaffProfile.objects.filter(user_id=instance.pk).delete()


def _sanitize_model_image(instance, field_name, **limits):
    field = getattr(instance, field_name, None)
    raw_upload = getattr(field, "_file", None)
    if raw_upload is None or getattr(raw_upload, "_animemo_sanitized", False):
        return
    try:
        sanitized = sanitize_uploaded_image(raw_upload, **limits)
    except DRFValidationError as error:
        raise DjangoValidationError({field_name: error.detail}) from error
    setattr(instance, field_name, sanitized)


@receiver(pre_save, sender=SiteSettings)
def sanitize_site_avatar(sender, instance, **_kwargs):
    from django.conf import settings

    _sanitize_model_image(
        instance,
        "site_avatar",
        max_bytes=settings.AVATAR_UPLOAD_MAX_BYTES,
        max_pixels=settings.AVATAR_UPLOAD_MAX_PIXELS,
        max_width=settings.AVATAR_UPLOAD_MAX_WIDTH,
        max_height=settings.AVATAR_UPLOAD_MAX_HEIGHT,
        output_max_width=1024,
        output_max_height=1024,
    )


@receiver(pre_save, sender=UserSettings)
def sanitize_user_avatar(sender, instance, **_kwargs):
    from django.conf import settings

    _sanitize_model_image(
        instance,
        "avatar",
        max_bytes=settings.AVATAR_UPLOAD_MAX_BYTES,
        max_pixels=settings.AVATAR_UPLOAD_MAX_PIXELS,
        max_width=settings.AVATAR_UPLOAD_MAX_WIDTH,
        max_height=settings.AVATAR_UPLOAD_MAX_HEIGHT,
        output_max_width=1024,
        output_max_height=1024,
    )


@receiver(pre_save, sender=JournalEntry)
def sanitize_entry_poster(sender, instance, **_kwargs):
    from django.conf import settings

    _sanitize_model_image(
        instance,
        "poster_file",
        max_bytes=settings.POSTER_UPLOAD_MAX_BYTES,
        max_pixels=settings.POSTER_UPLOAD_MAX_PIXELS,
        max_width=settings.POSTER_UPLOAD_MAX_WIDTH,
        max_height=settings.POSTER_UPLOAD_MAX_HEIGHT,
        output_max_width=1600,
        output_max_height=2400,
        output_quality=88,
    )


@receiver(pre_save, sender=Column)
def sanitize_column_cover(sender, instance, **_kwargs):
    from django.conf import settings

    _sanitize_model_image(
        instance,
        "cover",
        max_bytes=settings.COLUMN_COVER_UPLOAD_MAX_BYTES,
        max_pixels=settings.COLUMN_COVER_UPLOAD_MAX_PIXELS,
        max_width=settings.COLUMN_COVER_UPLOAD_MAX_WIDTH,
        max_height=settings.COLUMN_COVER_UPLOAD_MAX_HEIGHT,
        output_max_width=2400,
        output_max_height=2400,
    )


def _cleanup_model_file(sender, instance, field_name, **_kwargs):
    schedule_file_delete(
        getattr(instance, field_name, None),
        model_name=sender.__name__,
        object_id=getattr(instance, "pk", ""),
    )


@receiver(post_delete, sender=UserSettings)
def cleanup_user_avatar(sender, instance, **kwargs):
    _cleanup_model_file(sender, instance, "avatar", **kwargs)


@receiver(post_delete, sender=JournalEntry)
def cleanup_entry_poster(sender, instance, **kwargs):
    _cleanup_model_file(sender, instance, "poster_file", **kwargs)


@receiver(post_delete, sender=Column)
def cleanup_column_cover(sender, instance, **kwargs):
    _cleanup_model_file(sender, instance, "cover", **kwargs)


@receiver(post_delete, sender=SiteSettings)
def cleanup_site_avatar(sender, instance, **kwargs):
    _cleanup_model_file(sender, instance, "site_avatar", **kwargs)
