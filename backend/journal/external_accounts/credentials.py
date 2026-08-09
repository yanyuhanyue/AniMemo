import json

from config.credentials import (
    CredentialCipher,
    CredentialCipherError,
    CredentialDecryptionError,
)


class ExternalAccountCredentialError(ValueError):
    pass


def encrypt_credentials(payload):
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ExternalAccountCredentialError("外部账号凭据缺少 access token。")
    try:
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return CredentialCipher.encrypt(serialized)
    except (CredentialCipherError, TypeError, ValueError) as error:
        raise ExternalAccountCredentialError("外部账号凭据无法安全加密。") from error


def decrypt_credentials(ciphertext):
    try:
        payload = json.loads(CredentialCipher.decrypt(ciphertext))
    except (CredentialDecryptionError, CredentialCipherError, json.JSONDecodeError, TypeError) as error:
        raise ExternalAccountCredentialError("外部账号凭据无法解密，请重新授权。") from error
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ExternalAccountCredentialError("外部账号凭据格式无效，请重新授权。")
    return payload
