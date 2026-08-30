import json
import re

from rest_framework import serializers

from journal.models import JournalEntry
from journal.poster_security import PosterUrlValidationError, validate_poster_url

PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
MAX_IDENTITY_METADATA_BYTES = 64 * 1024


class RejectUnknownFieldsSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({
                    key: ["此字段不属于当前 Data Bundle schema。"]
                    for key in sorted(unknown)
                })
        return super().to_internal_value(data)


class EntryDataSerializer(RejectUnknownFieldsSerializer):
    title = serializers.CharField(max_length=200)
    japanese_title = serializers.CharField(max_length=200, allow_blank=True, default="")
    airing_period = serializers.CharField(max_length=50, allow_blank=True, default="")
    studio = serializers.CharField(max_length=120, allow_blank=True, default="")
    episodes = serializers.CharField(max_length=30, allow_blank=True, default="")
    description = serializers.CharField(allow_blank=True, default="")
    poster_url = serializers.URLField(max_length=1000, allow_blank=True, default="")
    custom_poster_url = serializers.URLField(max_length=1000, allow_blank=True, default="")
    baike_url = serializers.URLField(max_length=1000, allow_blank=True, default="")
    tags = serializers.ListField(
        child=serializers.CharField(max_length=100), max_length=30, default=list
    )
    tag_colors = serializers.DictField(
        child=serializers.CharField(max_length=20), default=dict
    )
    personal_score = serializers.DecimalField(
        max_digits=4, decimal_places=2, min_value=0, max_value=10,
        allow_null=True, required=False, default=None,
    )
    watch_status = serializers.ChoiceField(choices=JournalEntry.WatchStatus.choices)
    review = serializers.CharField(allow_blank=True, default="")
    visibility = serializers.ChoiceField(choices=JournalEntry.Visibility.choices)

    def validate_tags(self, value):
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))

    def validate_tag_colors(self, value):
        if len(value) > 30:
            raise serializers.ValidationError("标签颜色不能超过 30 项。")
        return value

    def validate_poster_url(self, value):
        return self._validate_poster(value)

    def validate_custom_poster_url(self, value):
        return self._validate_poster(value)

    @staticmethod
    def _validate_poster(value):
        try:
            return validate_poster_url(value)
        except PosterUrlValidationError as error:
            raise serializers.ValidationError("封面地址不符合安全策略。", code="invalid_poster_url") from error


class ExternalIdentityDataSerializer(RejectUnknownFieldsSerializer):
    provider = serializers.CharField(max_length=50)
    external_id = serializers.CharField(max_length=200)
    canonical_url = serializers.URLField(max_length=1000)
    metadata = serializers.JSONField(default=dict)
    metadata_schema_version = serializers.IntegerField(min_value=1, max_value=32767)
    is_metadata_source = serializers.BooleanField(default=False)
    metadata_fetched_at = serializers.DateTimeField(allow_null=True, required=False, default=None)
    provider_updated_at = serializers.DateTimeField(allow_null=True, required=False, default=None)

    def validate_provider(self, value):
        normalized = value.strip().lower()
        if not PROVIDER_RE.fullmatch(normalized):
            raise serializers.ValidationError("provider 格式无效。")
        return normalized

    def validate_external_id(self, value):
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("external_id 不能为空。")
        return normalized

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("metadata 必须是对象。")
        try:
            encoded = json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise serializers.ValidationError("metadata 必须是有效 JSON。") from error
        if len(encoded) > MAX_IDENTITY_METADATA_BYTES:
            raise serializers.ValidationError("metadata 不能超过 64 KiB。")
        return value


class BundleEntrySerializer(RejectUnknownFieldsSerializer):
    entry = EntryDataSerializer()
    external_identities = ExternalIdentityDataSerializer(many=True, default=list)
    watch_history = serializers.ListField(child=serializers.DictField(), default=list)

    def validate_external_identities(self, value):
        providers = [item["provider"] for item in value]
        if len(providers) != len(set(providers)):
            raise serializers.ValidationError("同一条目不能包含重复 provider。")
        if sum(bool(item["is_metadata_source"]) for item in value) > 1:
            raise serializers.ValidationError("同一条目最多只能有一个 metadata source。")
        return value


class DataBundleSerializer(RejectUnknownFieldsSerializer):
    format = serializers.CharField()
    schema_version = serializers.IntegerField()
    exported_at = serializers.DateTimeField()
    entries = BundleEntrySerializer(many=True, max_length=500)
