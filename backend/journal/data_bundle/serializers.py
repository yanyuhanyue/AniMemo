import json
import re

from rest_framework import serializers

from journal.entry_validation import normalize_entry_tags, validate_entry_tag_colors
from journal.serializers_entries import JournalEntrySerializer
from journal.watch_history.validation import HISTORY_CONTENT_FIELDS

PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")


class RejectUnknownFieldsMixin:
    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({
                    key: ["此字段不属于当前 Data Bundle schema。"]
                    for key in sorted(unknown, key=str)
                })
        return super().to_internal_value(data)


class RejectUnknownFieldsSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    pass


class EntryDataSerializer(RejectUnknownFieldsMixin, JournalEntrySerializer):
    class Meta(JournalEntrySerializer.Meta):
        fields = [
            "title", "japanese_title", "airing_period", "studio", "episodes",
            "description", "poster_url", "custom_poster_url", "baike_url", "tags",
            "tag_colors", "personal_score", "watch_status", "review", "visibility",
        ]
        read_only_fields = []

    def get_fields(self):
        fields = super().get_fields()
        for name, field in fields.items():
            if isinstance(field, serializers.CharField):
                field.trim_whitespace = False
            # Keep v1 required fields while obtaining optional defaults and limits
            # from the same model as normal writes. Expansion is budgeted preflight.
            field.required = name in {"title", "watch_status", "visibility"}
            if not field.required:
                field.default = self.Meta.model._meta.get_field(name).get_default
        return fields

    def validate_tags(self, value):
        return normalize_entry_tags(value, preserve=True)

    def validate_tag_colors(self, value):
        return validate_entry_tag_colors(value, preserve=True)


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
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise serializers.ValidationError("metadata 必须是有效 JSON。") from error
        # Accepted provider snapshots share the complete bundle's UTF-8 budget;
        # an additional ASCII-escaped cap would reject legal Unicode snapshots.
        return value


class BundleEntrySerializer(RejectUnknownFieldsSerializer):
    entry = EntryDataSerializer()
    external_identities = ExternalIdentityDataSerializer(many=True, default=list)
    watch_history = serializers.ListField(child=serializers.DictField(), default=list)

    def validate_watch_history(self, value):
        for index, record in enumerate(value):
            unknown = set(record) - set(HISTORY_CONTENT_FIELDS)
            if unknown:
                raise serializers.ValidationError({
                    index: {key: ["此字段不属于当前 Data Bundle schema。"] for key in sorted(unknown)}
                })
        return value

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
    entries = BundleEntrySerializer(many=True)
