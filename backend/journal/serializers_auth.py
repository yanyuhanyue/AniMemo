from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers


User = get_user_model()


class RegistrationRequestSerializer(serializers.Serializer):
    """The first registration step intentionally accepts only an email."""

    email = serializers.EmailField()

    def validate_email(self, value):
        return value.strip().casefold()


class RegistrationVerifySerializer(serializers.Serializer):
    token = serializers.CharField(trim_whitespace=True, write_only=True, max_length=512)


class RegistrationCompleteSerializer(serializers.Serializer):
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

