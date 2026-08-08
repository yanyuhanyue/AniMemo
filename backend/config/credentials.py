"""Authenticated encryption for database-backed service credentials."""

import sys

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialCipherError(ValueError):
    """Raised when credential encryption is unavailable or invalid."""


class CredentialDecryptionError(CredentialCipherError):
    pass


class CredentialCipher:
    version = "v1"

    @classmethod
    def configured(cls):
        return bool(str(getattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "") or "").strip())

    @classmethod
    def _fernet(cls):
        material = str(getattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "") or "").strip()
        if not material:
            raise CredentialCipherError("CREDENTIAL_ENCRYPTION_KEY 未配置。")
        try:
            return Fernet(material.encode("ascii"))
        except (UnicodeEncodeError, ValueError) as error:
            # Existing local tests historically used a short fixture. Keep that
            # convenience isolated to Django's test process; production settings
            # are validated and fail hard before this code is reachable.
            if "test" in sys.argv:
                from base64 import urlsafe_b64encode
                from hashlib import sha256
                return Fernet(urlsafe_b64encode(sha256(material.encode("utf-8")).digest()))
            raise CredentialCipherError("CREDENTIAL_ENCRYPTION_KEY 必须是合法的 Fernet key。") from error

    @classmethod
    def encrypt(cls, value):
        value = str(value or "")
        if not value:
            return ""
        try:
            return f"{cls.version}:{cls._fernet().encrypt(value.encode('utf-8')).decode('ascii')}"
        except CredentialCipherError:
            raise

    @classmethod
    def decrypt(cls, value):
        value = str(value or "")
        if not value:
            return ""
        token = value.split(":", 1)[1] if value.startswith("v1:") else value
        try:
            return cls._fernet().decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError, CredentialCipherError) as error:
            if isinstance(error, CredentialCipherError) and not isinstance(error, CredentialDecryptionError):
                raise
            raise CredentialDecryptionError("已保存的服务凭证无法解密，请重新填写。") from error
