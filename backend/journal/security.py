import base64
import hashlib
import hmac
import os
import secrets
import string
import struct
import time
from urllib.parse import quote, urlencode

from django.contrib.auth.hashers import check_password, make_password


TOTP_RECOVERY_CODE_COUNT = 6


def generate_totp_secret():
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _totp_at(secret, timestamp):
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode((secret + padding).upper())
    counter = int(timestamp // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret, code, *, timestamp=None, window=1):
    normalized = "".join(character for character in str(code or "") if character.isdigit())
    if len(normalized) != 6 or not secret:
        return False
    now = time.time() if timestamp is None else timestamp
    return any(hmac.compare_digest(_totp_at(secret, now + offset * 30), normalized) for offset in range(-window, window + 1))


def build_totp_uri(secret, account, issuer):
    label = f"{quote(str(issuer), safe='')}:{quote(str(account), safe='')}"
    query = urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": 6,
        "period": 30,
    })
    return f"otpauth://totp/{label}?{query}"


def generate_recovery_codes():
    alphabet = string.ascii_uppercase + string.digits
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
        for _ in range(TOTP_RECOVERY_CODE_COUNT)
    ]


def hash_recovery_codes(codes):
    return [make_password(str(code).strip().upper()) for code in codes]


def consume_recovery_code(profile, candidate, *, save=True):
    normalized = str(candidate or "").strip().upper()
    if not normalized:
        return False
    hashes = list(profile.recovery_code_hashes or [])
    for index, stored_hash in enumerate(hashes):
        if check_password(normalized, stored_hash):
            hashes.pop(index)
            profile.recovery_code_hashes = hashes
            if save:
                profile.save(update_fields=["recovery_code_hashes", "updated_at"])
            return True
    return False
