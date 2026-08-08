from rest_framework import serializers

from .models import ExternalIdentityBinding, IntegrationConnection


class IntegrationConnectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntegrationConnection
        fields = ("id", "provider", "instance_id", "name")
        read_only_fields = fields


class ExternalIdentityBindingSerializer(serializers.ModelSerializer):
    connection = IntegrationConnectionSerializer(read_only=True)

    class Meta:
        model = ExternalIdentityBinding
        fields = (
            "id",
            "connection",
            "platform",
            "external_user_id",
            "display_name",
            "enabled",
            "allow_group_delivery",
            "created_at",
            "verified_at",
        )
        read_only_fields = fields
