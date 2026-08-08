from config.credentials import CredentialCipher, CredentialDecryptionError


class SecretDecryptionError(ValueError):
    pass


def encrypt_secret(value):
    return CredentialCipher.encrypt(value)


def decrypt_secret(value):
    try:
        return CredentialCipher.decrypt(value)
    except CredentialDecryptionError as error:
        raise SecretDecryptionError("已保存的邮件密钥无法解密，请在管理员后台重新填写。") from error
