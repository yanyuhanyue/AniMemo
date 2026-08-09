from rest_framework import serializers

from .canonical import SUPPORTED_FIELDS


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({"non_field_errors": ["unsupported fields"]})
        return super().to_internal_value(data)


class CollectionSyncActionSerializer(StrictSerializer):
    field = serializers.ChoiceField(choices=SUPPORTED_FIELDS)
    action = serializers.ChoiceField(choices=("pull_remote", "accept_equal", "skip"))


class CollectionSyncApplySerializer(StrictSerializer):
    preview_token = serializers.CharField(min_length=1, max_length=4096, trim_whitespace=False)
    actions = CollectionSyncActionSerializer(many=True, allow_empty=True, max_length=3)

    def validate_actions(self, actions):
        fields = [item["field"] for item in actions]
        if len(fields) != len(set(fields)):
            raise serializers.ValidationError("duplicate field")
        return actions
