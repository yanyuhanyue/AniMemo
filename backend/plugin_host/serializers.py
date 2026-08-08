from rest_framework import serializers


class PluginInstallationUpdateSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    config = serializers.DictField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("至少提交 enabled 或 config。")
        return attrs
