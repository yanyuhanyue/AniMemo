from config.credentials import CredentialCipherError
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from journal.staff_services import StaffCapabilityPermission, record_audit

from .provider_configuration import (
    clear_provider_client_secret,
    get_effective_provider_configuration,
    update_provider_configuration,
)


class ProviderConfigurationUpdateSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False, allow_null=True)
    client_id = serializers.CharField(required=False, allow_blank=True, max_length=255, trim_whitespace=True)
    client_secret = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=4096,
        trim_whitespace=True,
        write_only=True,
    )

    def validate_client_secret(self, value):
        if not value:
            raise serializers.ValidationError("OAuth App Secret 不能为空；请使用清除操作移除数据库配置。")
        return value

    def validate(self, attrs):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({key: "不支持修改此字段。" for key in sorted(unknown)})
        return attrs


class StaffExternalProviderConfigurationView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def get(self, request, provider):
        try:
            configuration = get_effective_provider_configuration(provider)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        return Response(configuration.public_data())

    def patch(self, request, provider):
        serializer = ProviderConfigurationUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        fields = set(serializer.validated_data)
        if not fields:
            return Response({"detail": "请至少提供一个需要更新的配置项。"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            before = get_effective_provider_configuration(provider).public_data()
            configuration = update_provider_configuration(
                provider,
                enabled=serializer.validated_data.get("enabled"),
                client_id=serializer.validated_data.get("client_id", ""),
                client_secret=serializer.validated_data.get("client_secret", ""),
                fields=fields,
            )
        except CredentialCipherError:
            return Response({"detail": "服务凭据加密不可用，请检查服务器配置。"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        after = configuration.public_data()
        record_audit(
            request,
            action="provider_configuration.update",
            target_type="ExternalProviderConfiguration",
            target_id=configuration.provider,
            target_label=configuration.display_name,
            before=before,
            after=after,
            metadata={"updated_fields": sorted(fields)},
        )
        return Response(after)


class StaffExternalProviderClientSecretView(APIView):
    permission_classes = [StaffCapabilityPermission]
    required_capability = "manage_system"

    def delete(self, request, provider):
        try:
            before = get_effective_provider_configuration(provider).public_data()
            configuration = clear_provider_client_secret(provider)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        after = configuration.public_data()
        record_audit(
            request,
            action="provider_configuration.client_secret_cleared",
            target_type="ExternalProviderConfiguration",
            target_id=configuration.provider,
            target_label=configuration.display_name,
            before=before,
            after=after,
        )
        return Response(after)
