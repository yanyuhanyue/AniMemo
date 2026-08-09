from rest_framework import serializers


TOKEN_LOGIN_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["username", "password"],
    "properties": {
        "username": {"type": "string"},
        "password": {"type": "string", "writeOnly": True},
        "otp": {"type": "string", "writeOnly": True},
        "recovery_code": {"type": "string", "writeOnly": True},
        "cf-turnstile-response": {"type": "string", "writeOnly": True},
    },
}


class MessageResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()


class AuthUserResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()
    is_staff = serializers.BooleanField(required=False)
    role = serializers.CharField(required=False)
    capabilities = serializers.JSONField(required=False)
    pluginPermissions = serializers.ListField(child=serializers.CharField(), required=False)


class AccessTokenResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
    user = AuthUserResponseSerializer()


class LoginResponseSerializer(AccessTokenResponseSerializer):
    admin_url = serializers.URLField(required=False)
    admin_access = serializers.BooleanField(required=False)
    used_recovery_code = serializers.BooleanField(required=False)
    remaining_recovery_codes = serializers.IntegerField(required=False, allow_null=True)


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    cf_turnstile_response = serializers.CharField(required=False, allow_blank=True, write_only=True, source="cf-turnstile-response")


class PasswordResetConfirmRequestSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(write_only=True)
    password_confirm = serializers.CharField(write_only=True)
    cf_turnstile_response = serializers.CharField(required=False, allow_blank=True, write_only=True, source="cf-turnstile-response")


class PasswordChangeRequestSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True)
    password_confirm = serializers.CharField(write_only=True)


class AccountDeleteRequestSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    otp = serializers.CharField(required=False, allow_blank=True, write_only=True)
    recovery_code = serializers.CharField(required=False, allow_blank=True, write_only=True)


class CsrfTokenResponseSerializer(serializers.Serializer):
    csrf_token = serializers.CharField()


class RegistrationVerificationResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    completion_token = serializers.CharField()
    email = serializers.EmailField()


class ExternalAccountConnectRequestSerializer(serializers.Serializer):
    access_token = serializers.CharField(min_length=1, write_only=True)


class ExternalAccountAuthorizeResponseSerializer(serializers.Serializer):
    provider = serializers.CharField()
    authorization_url = serializers.URLField()


class ExternalMediaSearchResponseSerializer(serializers.Serializer):
    provider = serializers.CharField()
    results = serializers.ListField(child=serializers.JSONField())


class WatchHistoryCollectionResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = serializers.ListField(child=serializers.JSONField())


class WatchHistoryMutationResponseSerializer(serializers.Serializer):
    created = serializers.BooleanField(required=False)
    count = serializers.IntegerField(required=False)
    record = serializers.JSONField(required=False)
    results = serializers.ListField(child=serializers.JSONField(), required=False)


class ExternalImportPreviewRequestSerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, required=False, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False, default=24)
    filter = serializers.CharField(required=False, default="all")
    query = serializers.CharField(required=False, allow_blank=True, default="")


class ExternalImportApplyRequestSerializer(serializers.Serializer):
    preview_id = serializers.UUIDField()
    items = serializers.ListField(child=serializers.JSONField(), allow_empty=False)
