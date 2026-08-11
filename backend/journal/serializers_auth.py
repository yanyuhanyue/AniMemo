from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


User = get_user_model()


class AntiAbuseChallengeSerializer(serializers.Serializer):
    provider = serializers.CharField(max_length=40)
    token = serializers.CharField(write_only=True, max_length=4096)


@extend_schema_field({"type": "string", "writeOnly": True, "deprecated": True})
class LegacyTurnstileResponseField(serializers.CharField):
    pass


class AntiAbuseChallengeRequestSerializer(serializers.Serializer):
    challenge = AntiAbuseChallengeSerializer(required=False)

    def get_fields(self):
        fields = super().get_fields()
        fields["cf-turnstile-response"] = LegacyTurnstileResponseField(
            required=False,
            allow_blank=True,
            write_only=True,
        )
        return fields


class RegistrationRequestSerializer(AntiAbuseChallengeRequestSerializer):
    """The first registration step intentionally accepts only an email."""

    email = serializers.EmailField()

    def validate_email(self, value):
        return value.strip().casefold()


class RegistrationVerifySerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True, write_only=True, max_length=512)


class RegistrationCompleteSerializer(AntiAbuseChallengeRequestSerializer):
    completion_token = serializers.CharField(trim_whitespace=True, write_only=True, max_length=512)
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)

    def validate_username(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("用户名不能为空。")
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("该用户名已被使用。")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "两次输入的密码不一致。"})
        return attrs
