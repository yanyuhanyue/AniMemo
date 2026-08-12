from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


class FirstRunSetupSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=256, trim_whitespace=True, write_only=True)
    username = serializers.CharField(max_length=150, validators=[UnicodeUsernameValidator()])
    email = serializers.EmailField(required=True, allow_blank=False)
    password = serializers.CharField(max_length=256, trim_whitespace=False, write_only=True)
    password_confirm = serializers.CharField(max_length=256, trim_whitespace=False, write_only=True)

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": ["两次输入的密码不一致。"]})
        User = get_user_model()
        candidate = User(username=attrs["username"].strip(), email=attrs["email"].strip())
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({"password": list(error.messages)}) from error
        attrs["username"] = candidate.username
        attrs["email"] = candidate.email
        return attrs


class InstallationStatusSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=("uninitialized", "initializing", "initialized"))
    accepting_setup = serializers.BooleanField()
    expires_at = serializers.DateTimeField(allow_null=True)


class InstallationSetupResponseSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=("initialized",))
    detail = serializers.CharField()


class InstallationErrorResponseSerializer(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()
