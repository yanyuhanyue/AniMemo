import io
import logging
import secrets
import warnings

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers

ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
logger = logging.getLogger(__name__)


def _read_bounded(upload, max_bytes):
    declared_size = getattr(upload, "size", None)
    if declared_size is not None and declared_size > max_bytes:
        raise serializers.ValidationError(f"图片文件不能超过 {max_bytes // (1024 * 1024)}MB。")
    try:
        upload.seek(0)
        data = bytearray()
        while True:
            chunk = upload.read(min(64 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise serializers.ValidationError(f"图片文件不能超过 {max_bytes // (1024 * 1024)}MB。")
        return bytes(data)
    finally:
        try:
            upload.seek(0)
        except (AttributeError, OSError):
            pass


def sanitize_uploaded_image(
    upload,
    *,
    max_bytes,
    max_pixels,
    max_width,
    max_height,
    output_max_width,
    output_max_height,
    output_quality=85,
):
    raw = _read_bounded(upload, max_bytes)
    if not raw:
        raise serializers.ValidationError("上传文件不是有效图片。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            probe = Image.open(io.BytesIO(raw))
            source_format = str(probe.format or "").upper()
            width, height = probe.size
            if source_format not in ALLOWED_IMAGE_FORMATS:
                raise serializers.ValidationError("仅支持 JPG、PNG 或 WebP 图片。")
            if getattr(probe, "is_animated", False) or int(getattr(probe, "n_frames", 1)) != 1:
                raise serializers.ValidationError("不支持动态图片，请上传静态 JPG、PNG 或 WebP。")
            if width <= 0 or height <= 0:
                raise serializers.ValidationError("图片尺寸无效。")
            if width > max_width or height > max_height or width * height > max_pixels:
                raise serializers.ValidationError("图片像素或尺寸超过安全限制。")
            if max(width / height, height / width) > 16:
                raise serializers.ValidationError("图片宽高比超过安全限制。")
            probe.verify()

            image = Image.open(io.BytesIO(raw))
            if getattr(image, "is_animated", False) or int(getattr(image, "n_frames", 1)) != 1:
                raise serializers.ValidationError("不支持动态图片，请上传静态 JPG、PNG 或 WebP。")
            image.load()
            image = ImageOps.exif_transpose(image)
            if image.width * image.height > max_pixels:
                raise serializers.ValidationError("图片像素超过安全限制。")
            image.thumbnail((output_max_width, output_max_height), Image.Resampling.LANCZOS)
            has_alpha = "A" in image.getbands()
            image = image.convert("RGBA" if has_alpha else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=output_quality, method=6, exif=b"")
    except serializers.ValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError, ValueError) as error:
        raise serializers.ValidationError("上传文件不是有效图片。") from error
    sanitized = ContentFile(output.getvalue(), name=f"{secrets.token_hex(16)}.webp")
    sanitized._animemo_sanitized = True
    return sanitized


def delete_replaced_file(previous_file, current_file):
    if not previous_file:
        return
    previous_name = getattr(previous_file, "name", "")
    current_name = getattr(current_file, "name", "") if current_file else ""
    if previous_name and previous_name != current_name:
        storage = previous_file.storage

        def delete_after_commit():
            try:
                storage.delete(previous_name)
            except Exception as error:
                logger.warning(
                    "image_cleanup_failed",
                    extra={
                        "animemo_stage": "replaced_image_delete",
                        "animemo_exception_class": type(error).__name__,
                    },
                )

        transaction.on_commit(delete_after_commit)


def schedule_file_delete(file_field, *, model_name, object_id):
    """Delete a random, model-owned file only after its DB delete commits."""
    if not file_field:
        return
    name = str(getattr(file_field, "name", "") or "")
    if not name:
        return
    storage = file_field.storage

    def delete_after_commit():
        try:
            storage.delete(name)
        except Exception as error:
            logger.warning(
                "image_cleanup_failed",
                extra={
                    "animemo_stage": "model_image_delete",
                    "animemo_exception_class": type(error).__name__,
                },
            )

    transaction.on_commit(delete_after_commit)
