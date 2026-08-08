from config.credentials import CredentialCipher, CredentialDecryptionError


class IntegrationSecretError(ValueError):
    pass


def encrypt_connection_secret(secret):
    return CredentialCipher.encrypt(secret)


def decrypt_connection_secret(encrypted_secret):
    try:
        return CredentialCipher.decrypt(encrypted_secret)
    except CredentialDecryptionError as error:
        raise IntegrationSecretError("集成连接密钥无法解密，请轮换密钥。") from error
