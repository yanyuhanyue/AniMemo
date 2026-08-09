from rest_framework import serializers

from journal.models import WatchHistoryRecord

from .validation import WatchHistoryValidationError, normalize_watch_history_record


class WatchHistoryRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = WatchHistoryRecord
        fields = [
            "id",
            "watched_on",
            "watched_label",
            "brush_number",
            "brush_label",
            "episode_start",
            "episode_end",
            "notes",
            "metadata",
            "sequence",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class WatchHistoryWriteSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        try:
            return normalize_watch_history_record(data)
        except WatchHistoryValidationError as error:
            raise serializers.ValidationError({"code": error.code, "detail": error.detail}) from error


class WatchHistoryReplaceSerializer(serializers.Serializer):
    records = serializers.ListField(child=serializers.DictField(), max_length=500)
