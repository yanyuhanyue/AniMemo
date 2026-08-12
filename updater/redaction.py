from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import unquote_plus

_REDACTED = "[REDACTED]"
_MAX_JSON_DEPTH = 8

_SENSITIVE_EXACT_KEYS = {
    "authorization",
    "http_authorization",
    "proxy_authorization",
    "cookie",
    "http_cookie",
    "set_cookie",
    "credentials",
    "credential",
    "database_url",
    "db_url",
    "redis_url",
    "broker_url",
    "celery_broker_url",
    "dsn",
    "connection_string",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "secret_access_key",
    "access_key",
    "access_key_id",
    "azure_storage_key",
    "google_application_credentials",
    "service_account_key",
    "request_signature",
    "x_amz_signature",
    "x_amz_credential",
    "x_goog_signature",
    "x_goog_credential",
    "sessionid",
    "csrfmiddlewaretoken",
}
_SENSITIVE_SUFFIXES = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "signing_key",
    "encryption_key",
    "secret_key",
    "secret_access_key",
    "access_key_id",
    "account_key",
    "database_url",
    "db_url",
    "redis_url",
    "broker_url",
    "connection_string",
    "dsn",
)
_SENSITIVE_DERIVED_SUFFIXES = (
    "password_hash",
    "passwd_hash",
    "token_hash",
    "secret_hash",
)

_KEY = r"[A-Za-z][A-Za-z0-9_.-]{0,127}"
_QUOTED_VALUE = r'(?P<value_quote>["\'])(?P<value>(?:\\.|[^\\])*?)(?P=value_quote)'

_QUOTED_KEY_VALUE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?P<key_quote>[\"']?)(?P<key>{_KEY})(?P=key_quote)\s*[:=]\s*)"
    + _QUOTED_VALUE,
    re.DOTALL,
)
_ESCAPED_JSON_KEY_VALUE = re.compile(
    rf'(?P<prefix>\\"(?P<key>{_KEY})\\"\s*:\s*\\")(?P<value>.*?)(?P<suffix>(?<!\\)\\")',
    re.DOTALL,
)
_BARE_KEY_VALUE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?P<key_quote>[\"']?)(?P<key>{_KEY})(?P=key_quote)\s*[:=]\s*)"
    rf"(?P<value>(?![\"'])(?:\[REDACTED\]|(?:(?!\s+{_KEY}\s*[:=])[^,;}}&\]\r\n])+))",
)

_CLI_QUOTED = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_-])--(?P<key>{_KEY})(?:\s*=\s*|\s+))" + _QUOTED_VALUE,
    re.DOTALL,
)
_CLI_BARE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_-])--(?P<key>{_KEY})(?:\s*=\s*|\s+))"
    r"(?P<value>(?!--)[^\s,;]+)",
)

_AUTHORIZATION_LINE = re.compile(
    r"(?im)^(?P<prefix>\s*(?:(?:proxy|http)[-_])?authorization\s*:\s*)(?P<value>[^\r\n]*)"
)
_COOKIE_LINE = re.compile(
    r"(?im)^(?P<prefix>\s*(?P<header>set[-_]cookie|(?:http[-_])?cookie)\s*:\s*)(?P<value>[^\r\n]*)"
)
_COOKIE_INLINE = re.compile(
    r"(?i)(?P<prefix>\b(?P<header>set[-_]cookie|(?:http[-_])?cookie)\s*[:=]\s*)(?P<value>[^\r\n]*?)"
    r"(?=\s+(?:status|status_code|code|method|path|request_id|operation_id|exit_code)\s*[:=]|$)"
)
_AUTH_COMPLEX_INLINE = re.compile(
    r"(?i)(?P<prefix>\b(?:(?:proxy|http)[-_])?authorization\s*[:=]\s*)"
    r"(?P<scheme>digest|aws4-hmac-sha256)\s+(?P<credential>[^\r\n]+?)"
    r"(?=\s+(?:status|status_code|code|method|path|request_id|operation_id|exit_code)\s*[:=]|$)"
)
_AUTH_SIMPLE_INLINE = re.compile(
    r"(?i)(?P<prefix>\b(?:(?:proxy|http)[-_])?authorization\s*[:=]\s*)"
    r"(?P<scheme>bearer|basic|token|api-?key|jwt|negotiate|ntlm)\s+"
    r"(?P<credential>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\"']+)"
)
_AUTH_RAW_INLINE = re.compile(
    r"(?i)(?P<prefix>\b(?:(?:proxy|http)[-_])?authorization\s*[:=]\s*)"
    r"(?P<credential>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\"']+)"
)
_AUTH_SCHEMES = {
    "bearer",
    "basic",
    "token",
    "apikey",
    "api-key",
    "jwt",
    "negotiate",
    "ntlm",
    "digest",
    "aws4-hmac-sha256",
}

_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]{0,31}://)(?P<user>[^:/@\s?#]*):(?P<password>[^/@\s?#]+)@"
)
_URL_KEY = r"[A-Za-z%][A-Za-z0-9_.%+-]{0,191}"
_URL_PARAMETER = re.compile(rf"(?P<prefix>[?&#;](?P<key>{_URL_KEY})=)(?P<value>[^&#;\s]*)")
_SENSITIVE_URL_KEYS = {"sig", "signature"}
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_CLI_FLAG = re.compile(rf"^--(?P<key>{_KEY})(?P<equals>=?)(?P<value>.*)$")


def _normalise_key(key: object) -> str:
    text = str(key)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def _is_sensitive_key(key: object) -> bool:
    normalised = _normalise_key(key)
    if normalised in _SENSITIVE_EXACT_KEYS:
        return True
    if normalised.endswith(_SENSITIVE_DERIVED_SUFFIXES):
        return True
    return normalised.endswith(_SENSITIVE_SUFFIXES)


def _redact_json_text(value: str, depth: int) -> tuple[bool, str]:
    if depth >= _MAX_JSON_DEPTH:
        return True, _REDACTED
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False, value

    if isinstance(parsed, (Mapping, list)):
        scrubbed = _scrub_structure(parsed, depth + 1)
        return True, json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":"))
    if isinstance(parsed, str):
        nested, scrubbed = _redact_json_text(parsed, depth + 1)
        if nested:
            return True, json.dumps(scrubbed, ensure_ascii=False)
    return False, value


def _scrub_structure(value: object, depth: int = 0) -> object:
    if depth >= _MAX_JSON_DEPTH:
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _is_sensitive_key(key) else _scrub_structure(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return _scrub_sequence(value, depth)
    if isinstance(value, tuple):
        return tuple(_scrub_sequence(value, depth))
    if isinstance(value, str):
        parsed, scrubbed = _redact_json_text(value, depth + 1)
        return scrubbed if parsed else value
    return value


def _scrub_sequence(value: list[object] | tuple[object, ...], depth: int) -> list[object]:
    scrubbed: list[object] = []
    redact_next = False
    for item in value:
        if redact_next and not (isinstance(item, str) and item.startswith("--")):
            scrubbed.append(_REDACTED)
            redact_next = False
            continue
        redact_next = False

        if isinstance(item, str):
            flag = _CLI_FLAG.fullmatch(item)
            if flag and _is_sensitive_key(flag.group("key")):
                if flag.group("equals"):
                    scrubbed.append(f'--{flag.group("key")}={_REDACTED}')
                else:
                    scrubbed.append(item)
                    redact_next = True
                continue
        scrubbed.append(_scrub_structure(item, depth + 1))
    return scrubbed


def _replace_secret_value(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return _replace_nested_json_value(match)
    if (
        not match.groupdict().get("value_quote")
        and not match.group("prefix").lstrip().startswith("--")
        and _normalise_key(match.group("key"))
        in {
            "authorization",
            "http_authorization",
            "proxy_authorization",
            "cookie",
            "http_cookie",
            "set_cookie",
        }
    ):
        # Header-shaped text is handled first by the dedicated rules. Avoid
        # treating a redacted cookie list or an authorization scheme as one
        # generic scalar and destroying useful header diagnostics.
        return match.group(0)
    quote = match.groupdict().get("value_quote") or ""
    return f'{match.group("prefix")}{quote}{_REDACTED}{quote}'


def _replace_nested_json_value(match: re.Match[str]) -> str:
    quote = match.groupdict().get("value_quote")
    if not quote:
        return match.group(0)
    raw = match.group("value")
    try:
        if quote == '"':
            decoded = json.loads(f'"{raw}"')
        else:
            decoded = raw.replace("\\'", "'")
    except (TypeError, ValueError):
        return match.group(0)

    parsed, scrubbed = _redact_json_text(decoded, 0)
    if not parsed:
        return match.group(0)
    if quote == '"':
        encoded = json.dumps(scrubbed, ensure_ascii=False)[1:-1]
    else:
        encoded = scrubbed.replace("\\", "\\\\").replace("'", "\\'")
    return f'{match.group("prefix")}{quote}{encoded}{quote}'


def _replace_escaped_json_secret(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    return f'{match.group("prefix")}{_REDACTED}{match.group("suffix")}'


def _redact_authorization_value(value: str) -> str:
    stripped = value.strip()
    quote = ""
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        quote = stripped[0]
        stripped = stripped[1:-1].strip()
    scheme_match = re.match(r"([A-Za-z][A-Za-z0-9-]*)\b", stripped)
    scheme = scheme_match.group(1) if scheme_match and scheme_match.group(1).lower() in _AUTH_SCHEMES else ""
    redacted = f"{scheme} {_REDACTED}" if scheme else _REDACTED
    return f"{quote}{redacted}{quote}" if quote else redacted


def _replace_authorization_line(match: re.Match[str]) -> str:
    return f'{match.group("prefix")}{_redact_authorization_value(match.group("value"))}'


def _replace_complex_authorization(match: re.Match[str]) -> str:
    return f'{match.group("prefix")}{match.group("scheme")} {_REDACTED}'


def _replace_simple_authorization(match: re.Match[str]) -> str:
    return f'{match.group("prefix")}{match.group("scheme")} {_REDACTED}'


def _replace_raw_authorization(match: re.Match[str]) -> str:
    credential = match.group("credential")
    if credential.strip("\"'").lower() in _AUTH_SCHEMES:
        return match.group(0)
    return f'{match.group("prefix")}{_REDACTED}'


def _redact_cookie_pair(part: str) -> str:
    match = re.match(r"(?P<prefix>\s*[^=;\s]+\s*=\s*)(?P<value>.*?)(?P<trailing>\s*)$", part)
    if not match:
        return part
    value = match.group("value")
    outer_quote = ""
    if value and value[-1] in "\"'" and (not value.startswith(value[-1]) or len(value) == 1):
        outer_quote = value[-1]
        value = value[:-1]
    quote = value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else ""
    replacement = f"{quote}{_REDACTED}{quote}" if quote else _REDACTED
    return f'{match.group("prefix")}{replacement}{outer_quote}{match.group("trailing")}'


def _replace_cookie_line(match: re.Match[str]) -> str:
    parts = match.group("value").split(";")
    if _normalise_key(match.group("header")) == "set_cookie":
        parts[0] = _redact_cookie_pair(parts[0])
    else:
        parts = [_redact_cookie_pair(part) for part in parts]
    return f'{match.group("prefix")}{";".join(parts)}'


def _replace_url_parameter(match: re.Match[str]) -> str:
    key = unquote_plus(match.group("key"))
    if not _is_sensitive_key(key) and _normalise_key(key) not in _SENSITIVE_URL_KEYS:
        return match.group(0)
    return f'{match.group("prefix")}{_REDACTED}'


def redact(value: object) -> str:
    """Return a diagnostic string with credential material removed.

    The function intentionally keeps non-secret identifiers and surrounding log
    context. It accepts structured Python values as well as plain or JSON text.
    """

    if isinstance(value, (Mapping, list, tuple)):
        text = str(_scrub_structure(value))
    else:
        text = str(value)
        parsed, scrubbed = _redact_json_text(text, 0)
        if parsed:
            text = scrubbed

    text = _PEM_PRIVATE_KEY.sub(_REDACTED, text)
    text = _AUTHORIZATION_LINE.sub(_replace_authorization_line, text)
    text = _COOKIE_LINE.sub(_replace_cookie_line, text)
    text = _COOKIE_INLINE.sub(_replace_cookie_line, text)
    text = _AUTH_COMPLEX_INLINE.sub(_replace_complex_authorization, text)
    text = _AUTH_SIMPLE_INLINE.sub(_replace_simple_authorization, text)
    text = _AUTH_RAW_INLINE.sub(_replace_raw_authorization, text)
    text = _CLI_QUOTED.sub(_replace_secret_value, text)
    text = _CLI_BARE.sub(_replace_secret_value, text)
    text = _QUOTED_KEY_VALUE.sub(_replace_secret_value, text)
    text = _ESCAPED_JSON_KEY_VALUE.sub(_replace_escaped_json_secret, text)
    text = _BARE_KEY_VALUE.sub(_replace_secret_value, text)
    text = _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:[REDACTED]@", text)
    return _URL_PARAMETER.sub(_replace_url_parameter, text)
