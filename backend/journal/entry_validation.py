from rest_framework import serializers


def normalize_entry_tags(value, *, preserve=False):
    """Share the stored tag type without discarding accepted portable values."""
    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
        raise serializers.ValidationError("标签必须是字符串数组。")
    if preserve:
        return value
    return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))


def validate_entry_tag_colors(value, *, preserve=False):
    if isinstance(value, dict):
        return value
    if preserve:
        # Older writes accepted empty JSON values and key/value-pair arrays.
        # Keep their stored JSON intact when the existing Core DTO can read it.
        try:
            dict(value or {})
        except (TypeError, ValueError):
            pass
        else:
            return value
    raise serializers.ValidationError("标签颜色必须是 JSON 对象。")
