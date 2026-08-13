from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import unquote_plus

_REDACTED = "[REDACTED]"
_MAX_JSON_DEPTH = 8
_MAX_PERCENT_DECODE_DEPTH = 4

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
    "setup_code",
    "bootstrap_code",
    "recovery_code",
    "otp",
    "totp",
    "passphrase",
    "private_key_passphrase",
    "client_assertion",
    "refresh_credential",
    "access_credential",
    "session_key",
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
_SAFE_DIAGNOSTIC_FIELD = (
    r"(?:status|status_code|code|method|path|request_id|operation_id|exit_code)"
)
_QUOTED_VALUE = r'(?P<value_quote>["\'])(?P<value>(?:\\.|[^\\])*?)(?P=value_quote)'

_QUOTED_KEY_VALUE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])[\"']?(?P<key>{_KEY})[\"']?\s*[:=]\s*)"
    + _QUOTED_VALUE,
    re.DOTALL,
)
_ESCAPED_JSON_KEY_PREFIX = re.compile(
    rf'(?<!\\)\\+"(?P<key>{_KEY})(?<!\\)\\+"\s*:\s*'
    r'(?<!\\)(?P<value_escape>\\+)"'
)
_TRUNCATED_QUOTED_KEY_VALUE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])[\"']?(?P<key>{_KEY})[\"']?\s*[:=]\s*)"
    r"(?P<value_quote>[\"'])(?P<value>(?:\\.|(?!(?P=value_quote))[^\\])*)[\\]?\Z",
    re.DOTALL,
)
_BARE_KEY_PREFIX = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])[\"']?(?P<key>{_KEY})[\"']?\s*[:=]\s*)"
)

_CLI_QUOTED = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_-])--(?P<key>{_KEY})(?:\s*=\s*|\s+))" + _QUOTED_VALUE,
    re.DOTALL,
)
_TRUNCATED_CLI_QUOTED = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_-])--(?P<key>{_KEY})(?:\s*=\s*|\s+))"
    r"(?P<value_quote>[\"'])(?P<value>(?:\\.|(?!(?P=value_quote))[^\\])*)[\\]?\Z",
    re.DOTALL,
)
_CLI_BARE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_-])--(?P<key>{_KEY})(?:\s*=\s*|\s+))"
    r"(?P<value>(?!--)[^\r\n]*?)"
    rf"(?=(?:[ \t]+--{_KEY}(?:[ \t]*=|[ \t]+|\Z))|"
    rf"(?:[ \t]+{_SAFE_DIAGNOSTIC_FIELD}[ \t]*[:=])|"
    rf"(?:[\"']\s*,\s*[\"']--{_KEY}(?:[\"'= \t]))|"
    r"(?:[\"']\s*\])|\r?$)",
    re.MULTILINE,
)

_COOKIE_LINE = re.compile(
    r"(?im)^(?P<prefix>[ \t]*(?P<header>set[-_]cookie|(?:http[-_])?cookie)[ \t]*:[ \t]*)"
    rf"(?P<value>[^\r\n]*?)(?=(?<![;,\s])[ \t]+"
    rf"{_SAFE_DIAGNOSTIC_FIELD}[ \t]*[:=]|\r?$)"
)
_COOKIE_INLINE = re.compile(
    r"(?i)(?P<prefix>\b(?P<header>set[-_]cookie|(?:http[-_])?cookie)[ \t]*[:=][ \t]*)"
    rf"(?P<value>[^\r\n]*?)(?=(?<![;,\s])[ \t]+"
    rf"{_SAFE_DIAGNOSTIC_FIELD}[ \t]*[:=]|\r?\n|\Z)"
)
_COOKIE_PAIR_START = re.compile(r"\s*[!#$%&'*+\-.^_`|~0-9A-Za-z]+\s*=")
_COOKIE_PAIR = re.compile(
    r"(?P<prefix>\s*[^=;\s]+\s*=\s*)(?P<value>.*?)(?P<trailing>\s*)$"
)
_COOKIE_OBJECT_FIELDS = {
    "name",
    "key",
    "value",
    "coded_value",
    "domain",
    "path",
    "expires",
    "max_age",
    "secure",
    "http_only",
    "httponly",
    "same_site",
    "samesite",
    "partitioned",
}
_COOKIE_VALUE_FIELDS = {"value", "coded_value"}
_COOKIE_CONTAINER_KEYS = {
    "cookies",
    "cookie_jar",
    "cookiejar",
    "request_cookies",
    "response_cookies",
}
_COOKIE_HEADER_KEYS = {"cookie", "http_cookie", "set_cookie"}
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
_AUTHORIZATION_PREFIX = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:(?:proxy|http)[-_])?authorization[ \t]*"
    r"(?P<separator>[:=])[ \t]*"
)
_LINE_END = re.compile(r"\r\n?|\n")
_AUTH_SCHEME_TOKEN = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*)")
_SAFE_DIAGNOSTIC_BOUNDARY = re.compile(
    rf"\s+{_SAFE_DIAGNOSTIC_FIELD}\s*[:=]",
    re.IGNORECASE,
)

_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]{0,31}://)(?P<user>[^:/@\s?#]*):(?P<password>[^/@\s?#]+)@"
)
_URL_KEY = r"[A-Za-z%][A-Za-z0-9_.%+-]{0,191}"
_URL_PARAMETER = re.compile(
    rf"(?P<prefix>[?&#;](?P<key>{_URL_KEY})=)(?P<value>[^&#;\s]*)"
)
_SENSITIVE_URL_KEYS = {"sig", "signature"}
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?P<private_key_label>(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----"
    r".*?(?:-----END (?P=private_key_label)-----|\Z)",
    re.DOTALL,
)
_TRUNCATED_PRIVATE_KEY_BEGIN = re.compile(
    r"-----BEGIN (?=[A-Z0-9 -]{0,128}(?:\r?\n|\Z))"
    r"(?=[^\r\n]{0,128}PRIV)[A-Z0-9 -]{0,128}(?:\r?\n|\Z).*\Z",
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


def _contains_sensitive_syntax(value: str) -> bool:
    """Return whether normal redactors would remove material from decoded text."""

    if _PRIVATE_KEY_BLOCK.sub(_REDACTED, value) != value:
        return True
    if _TRUNCATED_PRIVATE_KEY_BEGIN.sub(_REDACTED, value) != value:
        return True
    if _redact_authorization_text(value) != value:
        return True
    for pattern in (_COOKIE_LINE, _COOKIE_INLINE):
        for match in pattern.finditer(value):
            if _redact_cookie_value(
                match.group("header"),
                match.group("value"),
                inspect_percent_encoded=False,
            ) != match.group("value"):
                return True
    if _CLI_QUOTED.sub(_replace_secret_value, value) != value:
        return True
    if _TRUNCATED_CLI_QUOTED.sub(_replace_truncated_secret_value, value) != value:
        return True
    if _CLI_BARE.sub(_replace_secret_value, value) != value:
        return True
    if _QUOTED_KEY_VALUE.sub(_replace_secret_value, value) != value:
        return True
    if _redact_escaped_json_text(value) != value:
        return True
    for match in _URL_CREDENTIALS.finditer(value):
        if match.group("password") != _REDACTED:
            return True
    if _URL_PARAMETER.sub(_replace_unencoded_url_parameter, value) != value:
        return True
    return _redact_bare_key_values(value) != value


def _percent_decoding_reveals_sensitive_material(value: str) -> bool:
    """Inspect bounded URL-decoding layers and fail closed beyond the bound."""

    if "%" not in value:
        return False
    decoded = value
    for _ in range(_MAX_PERCENT_DECODE_DEPTH):
        next_value = unquote_plus(decoded)
        if next_value == decoded:
            return False
        if _contains_sensitive_syntax(next_value):
            return True
        decoded = next_value
    return unquote_plus(decoded) != decoded


def _structure_exceeds_depth(value: object, depth: int) -> bool:
    """Check parsed JSON depth without relying on the interpreter recursion limit."""

    if depth >= _MAX_JSON_DEPTH:
        return True
    if isinstance(value, Mapping):
        children = iter(value.values())
    elif isinstance(value, (list, tuple)):
        children = iter(value)
    else:
        return False

    stack: list[tuple[object, int]] = [(children, depth + 1)]
    while stack:
        children, child_depth = stack[-1]
        try:
            child = next(children)
        except StopIteration:
            stack.pop()
            continue
        if child_depth >= _MAX_JSON_DEPTH:
            return True
        if isinstance(child, Mapping):
            stack.append((iter(child.values()), child_depth + 1))
        elif isinstance(child, (list, tuple)):
            stack.append((iter(child), child_depth + 1))
    return False


def _redact_json_text(value: str, depth: int) -> tuple[bool, str]:
    if depth >= _MAX_JSON_DEPTH:
        return True, _REDACTED
    try:
        parsed = json.loads(value)
    except RecursionError:
        return True, _REDACTED
    except (TypeError, ValueError):
        return False, value

    if isinstance(parsed, (Mapping, list)):
        if _structure_exceeds_depth(parsed, depth + 1):
            return True, _REDACTED
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
        scrubbed: dict[object, object] = {}
        for key, item in value.items():
            normalised = _normalise_key(key)
            if normalised in _COOKIE_CONTAINER_KEYS:
                scrubbed[key] = _scrub_cookie_container(item, depth + 1)
            elif normalised in _COOKIE_HEADER_KEYS:
                if isinstance(item, (Mapping, list, tuple)):
                    scrubbed[key] = _scrub_cookie_container(item, depth + 1)
                else:
                    scrubbed[key] = _redact_cookie_value(normalised, str(item))
            elif _is_sensitive_key(key):
                scrubbed[key] = _REDACTED
            else:
                scrubbed[key] = _scrub_structure(item, depth + 1)
        return scrubbed
    if isinstance(value, list):
        return _scrub_sequence(value, depth)
    if isinstance(value, tuple):
        return tuple(_scrub_sequence(value, depth))
    if isinstance(value, str):
        parsed, scrubbed = _redact_json_text(value, depth + 1)
        return scrubbed if parsed else value
    return value


def _scrub_cookie_container(value: object, depth: int) -> object:
    if depth >= _MAX_JSON_DEPTH:
        return _REDACTED
    if isinstance(value, Mapping):
        normalised_keys = {_normalise_key(key) for key in value}
        if (
            normalised_keys & _COOKIE_VALUE_FIELDS
            and normalised_keys <= _COOKIE_OBJECT_FIELDS
        ):
            return {
                key: (
                    _REDACTED
                    if _normalise_key(key) in _COOKIE_VALUE_FIELDS
                    else _scrub_structure(item, depth + 1)
                )
                for key, item in value.items()
            }
        return {
            key: (
                _scrub_cookie_container(item, depth + 1)
                if isinstance(item, (Mapping, list, tuple))
                else _REDACTED
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_cookie_container(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_cookie_container(item, depth + 1) for item in value)
    return _REDACTED


def _scrub_sequence(
    value: list[object] | tuple[object, ...], depth: int
) -> list[object]:
    if (
        len(value) == 2
        and isinstance(value[0], str)
        and _normalise_key(value[0]) in _COOKIE_HEADER_KEYS
    ):
        header = value[0]
        item = value[1]
        if isinstance(item, (Mapping, list, tuple)):
            scrubbed_item = _scrub_cookie_container(item, depth + 1)
        else:
            scrubbed_item = _redact_cookie_value(header, str(item))
        return [header, scrubbed_item]

    if len(value) == 2 and isinstance(value[0], str) and _is_sensitive_key(value[0]):
        key = value[0]
        item = value[1]
        if _normalise_key(key) in {
            "authorization",
            "http_authorization",
            "proxy_authorization",
        }:
            scrubbed_item = _redact_arbitrary_authorization_value(str(item))
        else:
            scrubbed_item = _REDACTED
        return [key, scrubbed_item]

    if value and all(
        isinstance(item, Mapping)
        and bool({_normalise_key(key) for key in item} & _COOKIE_VALUE_FIELDS)
        and {_normalise_key(key) for key in item} <= _COOKIE_OBJECT_FIELDS
        for item in value
    ):
        return [_scrub_cookie_container(item, depth + 1) for item in value]

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
                    scrubbed.append(f"--{flag.group('key')}={_REDACTED}")
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
    return f"{match.group('prefix')}{quote}{_REDACTED}{quote}"


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
    return f"{match.group('prefix')}{quote}{encoded}{quote}"


def _replace_truncated_secret_value(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    quote = match.groupdict().get("value_quote") or ""
    return f"{match.group('prefix')}{quote}{_REDACTED}"


def _bare_secret_value_end(text: str, start: int) -> int:
    line_end, _ = _line_end(text, start)
    boundary = _SAFE_DIAGNOSTIC_BOUNDARY.search(text, start, line_end)
    return boundary.start() if boundary else line_end


def _redacted_marker_is_complete(text: str, start: int) -> bool:
    end = start + len(_REDACTED)
    line_end, _ = _line_end(text, end)
    if end >= line_end:
        return True
    remainder = text[end:line_end]
    if _SAFE_DIAGNOSTIC_BOUNDARY.match(remainder):
        return True
    return False


def _trusted_redacted_value_starts(text: str) -> set[int]:
    """Locate exact values redacted by the cookie and URL parsers."""

    starts: set[int] = set()
    for pattern in (_COOKIE_LINE, _COOKIE_INLINE):
        for match in pattern.finditer(text):
            value = match.group("value")
            value_start = match.start("value")
            if _normalise_key(match.group("header")) == "set_cookie":
                for record_start, record_end in _split_combined_set_cookie_spans(
                    value
                ):
                    record = value[record_start:record_end]
                    first_part = record.split(";", 1)[0]
                    pair = _COOKIE_PAIR.match(first_part)
                    if not pair:
                        continue
                    marker_start = value_start + record_start + pair.start("value")
                    if text.startswith(_REDACTED, marker_start):
                        starts.add(marker_start)
            else:
                for pair in _BARE_KEY_PREFIX.finditer(value):
                    marker_start = value_start + pair.end()
                    if text.startswith(_REDACTED, marker_start):
                        starts.add(marker_start)

    for match in _URL_PARAMETER.finditer(text):
        key = unquote_plus(match.group("key"))
        if not _is_sensitive_key(key) and _normalise_key(key) not in _SENSITIVE_URL_KEYS:
            continue
        marker_start = match.start("value")
        if text.startswith(_REDACTED, marker_start):
            starts.add(marker_start)
    return starts


def _redact_bare_key_values(text: str) -> str:
    output: list[str] = []
    output_cursor = 0
    search_cursor = 0
    trusted_redacted_starts = _trusted_redacted_value_starts(text)
    while match := _BARE_KEY_PREFIX.search(text, search_cursor):
        value_start = match.end()
        search_cursor = value_start
        normalised_key = _normalise_key(match.group("key"))
        if not _is_sensitive_key(match.group("key")):
            continue
        if normalised_key in {
            "authorization",
            "http_authorization",
            "proxy_authorization",
            "cookie",
            "http_cookie",
            "set_cookie",
        }:
            # Dedicated header rules run first and preserve safe scheme, cookie
            # names, and attributes. A generic second pass would destroy that
            # useful diagnostic structure or split the redaction marker.
            continue
        if value_start >= len(text) or text[value_start] in "\"'":
            continue
        if text.startswith(_REDACTED, value_start) and (
            value_start in trusted_redacted_starts
            or _redacted_marker_is_complete(text, value_start)
        ):
            search_cursor = value_start + len(_REDACTED)
            continue

        value_end = _bare_secret_value_end(text, value_start)
        if value_end <= value_start:
            continue
        output.append(text[output_cursor : match.end()])
        output.append(_REDACTED)
        output_cursor = value_end
        search_cursor = value_end

    output.append(text[output_cursor:])
    return "".join(output)


def _redact_escaped_json_text(text: str) -> str:
    output: list[str] = []
    output_cursor = 0
    search_cursor = 0
    while match := _ESCAPED_JSON_KEY_PREFIX.search(text, search_cursor):
        search_cursor = match.end()
        if not _is_sensitive_key(match.group("key")):
            continue

        delimiter = f'{match.group("value_escape")}"'
        closing = text.find(delimiter, match.end())
        while closing >= 0 and closing > 0 and text[closing - 1] == "\\":
            closing = text.find(delimiter, closing + 1)

        output.append(text[output_cursor : match.end()])
        output.append(_REDACTED)
        if closing < 0:
            return "".join(output)
        output.append(delimiter)
        output_cursor = closing + len(delimiter)
        search_cursor = output_cursor

    output.append(text[output_cursor:])
    return "".join(output)


def _authorization_value_end(line: str, prefix_start: int, value_start: int) -> int:
    outer_quote = (
        line[prefix_start - 1]
        if prefix_start > 0 and line[prefix_start - 1] in "\"'"
        else ""
    )
    quote = ""
    escaped = False
    for index in range(value_start, len(line)):
        character = line[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if outer_quote:
            if character == outer_quote:
                return index
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            continue
        if character.isspace() and _SAFE_DIAGNOSTIC_BOUNDARY.match(line, index):
            previous = index - 1
            while previous >= value_start and line[previous].isspace():
                previous -= 1
            if previous < value_start or line[previous] not in ",;":
                return index
    return len(line)


def _redact_arbitrary_authorization_value(value: str) -> str:
    stripped = value.strip()
    wrapper = ""
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        wrapper = stripped[0]
        stripped = stripped[1:-1].strip()
    scheme_match = _AUTH_SCHEME_TOKEN.match(stripped)
    scheme = ""
    if scheme_match:
        remainder = stripped[scheme_match.end() :].lstrip()
        candidate = scheme_match.group("scheme")
        if remainder or candidate.lower() in _AUTH_SCHEMES:
            scheme = candidate
    redacted = f"{scheme} {_REDACTED}" if scheme else _REDACTED
    return f"{wrapper}{redacted}{wrapper}" if wrapper else redacted


def _line_end(text: str, start: int) -> tuple[int, int]:
    match = _LINE_END.search(text, start)
    if not match:
        return len(text), len(text)
    return match.start(), match.end()


def _redact_authorization_line(line: str) -> tuple[str, bool]:
    continuation_candidate = False
    cursor = 0
    rendered: list[str] = []
    while match := _AUTHORIZATION_PREFIX.search(line, cursor):
        rendered.append(line[cursor : match.end()])
        value_end = _authorization_value_end(line, match.start(), match.end())
        rendered.append(
            _redact_arbitrary_authorization_value(line[match.end() : value_end])
        )
        continuation_candidate = match.group("separator") == ":" and value_end == len(
            line
        )
        cursor = value_end
    rendered.append(line[cursor:])
    return "".join(rendered), continuation_candidate


def _redact_authorization_text(text: str) -> str:
    output: list[str] = []
    output_cursor = 0
    search_cursor = 0
    while match := _AUTHORIZATION_PREFIX.search(text, search_cursor):
        previous_carriage_return = text.rfind("\r", search_cursor, match.start())
        previous_line_feed = text.rfind("\n", search_cursor, match.start())
        previous_line_end = max(previous_carriage_return, previous_line_feed)
        line_start = previous_line_end + 1 if previous_line_end >= 0 else search_cursor
        content_end, processed_end = _line_end(text, match.end())
        rendered, redact_continuation = _redact_authorization_line(
            text[line_start:content_end]
        )

        output.append(text[output_cursor:line_start])
        output.append(rendered)
        output.append(text[content_end:processed_end])

        while (
            redact_continuation
            and processed_end < len(text)
            and text[processed_end] in " \t"
        ):
            continuation_end, ending_end = _line_end(text, processed_end)
            continuation = text[processed_end:continuation_end]
            indentation = continuation[
                : len(continuation) - len(continuation.lstrip(" \t"))
            ]
            output.append(f"{indentation}{_REDACTED}")
            output.append(text[continuation_end:ending_end])
            processed_end = ending_end

        output_cursor = processed_end
        search_cursor = processed_end

    output.append(text[output_cursor:])
    return "".join(output)


def _redact_cookie_pair(part: str) -> str:
    match = _COOKIE_PAIR.match(part)
    if not match:
        return part
    value = match.group("value")
    outer_quote = ""
    if (
        value
        and value[-1] in "\"'"
        and (not value.startswith(value[-1]) or len(value) == 1)
    ):
        outer_quote = value[-1]
        value = value[:-1]
    quote = (
        value[0]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        else ""
    )
    replacement = f"{quote}{_REDACTED}{quote}" if quote else _REDACTED
    return f"{match.group('prefix')}{replacement}{outer_quote}{match.group('trailing')}"


def _redact_cookie_scalar(value: str) -> str:
    leading = value[: len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()) :]
    stripped = value.strip()
    wrapper = ""
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        wrapper = stripped[0]
        return f"{leading}{wrapper}{_REDACTED}{wrapper}{trailing}"
    outer_quote = stripped[-1] if stripped.endswith(('"', "'")) else ""
    return f"{leading}{_REDACTED}{outer_quote}{trailing}"


def _split_combined_set_cookie_spans(value: str) -> list[tuple[int, int]]:
    records: list[tuple[int, int]] = []
    start = 0
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            continue
        if character == "," and _COOKIE_PAIR_START.match(value, index + 1):
            records.append((start, index))
            start = index + 1
    records.append((start, len(value)))
    return records


def _split_combined_set_cookie(value: str) -> list[str]:
    return [
        value[start:end] for start, end in _split_combined_set_cookie_spans(value)
    ]


def _redact_set_cookie_record(
    record: str, *, inspect_percent_encoded: bool = True
) -> str:
    parts = record.split(";")
    if _COOKIE_PAIR.match(parts[0]):
        redacted = _redact_cookie_pair(parts[0])
    else:
        redacted = _redact_cookie_scalar(parts[0])
    parts[0] = redacted
    if inspect_percent_encoded:
        for index in range(1, len(parts)):
            attribute = _COOKIE_PAIR.match(parts[index])
            if attribute and _percent_decoding_reveals_sensitive_material(
                attribute.group("value")
            ):
                parts[index] = _redact_cookie_pair(parts[index])
    return ";".join(parts)


def _redact_cookie_record(record: str) -> str:
    parts = record.split(";")
    redacted: list[str] = []
    for part in parts:
        if not part.strip():
            redacted.append(part)
        elif _COOKIE_PAIR.match(part):
            redacted.append(_redact_cookie_pair(part))
        else:
            redacted.append(_redact_cookie_scalar(part))
    return ";".join(redacted)


def _replace_cookie_line(match: re.Match[str]) -> str:
    return (
        f"{match.group('prefix')}"
        f"{_redact_cookie_value(match.group('header'), match.group('value'))}"
    )


def _redact_cookie_value(
    header: object,
    value: str,
    *,
    inspect_percent_encoded: bool = True,
) -> str:
    if _normalise_key(header) == "set_cookie":
        redacted = ",".join(
            _redact_set_cookie_record(
                record, inspect_percent_encoded=inspect_percent_encoded
            )
            for record in _split_combined_set_cookie(value)
        )
    else:
        redacted = ",".join(
            _redact_cookie_record(record)
            for record in _split_combined_set_cookie(value)
        )
    return redacted


def _replace_unencoded_url_parameter(match: re.Match[str]) -> str:
    key = unquote_plus(match.group("key"))
    if not _is_sensitive_key(key) and _normalise_key(key) not in _SENSITIVE_URL_KEYS:
        return match.group(0)
    return f"{match.group('prefix')}{_REDACTED}"


def _replace_url_parameter(match: re.Match[str]) -> str:
    replacement = _replace_unencoded_url_parameter(match)
    if replacement != match.group(0):
        return replacement
    if _percent_decoding_reveals_sensitive_material(match.group("value")):
        return f"{match.group('prefix')}{_REDACTED}"
    return match.group(0)


def _redact_percent_encoded_text(text: str) -> str:
    """Fail closed for encoded credentials outside syntax-specific containers."""

    if "%" not in text:
        return text
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        if _percent_decoding_reveals_sensitive_material(content):
            output.append(f"{_REDACTED}{ending}")
        else:
            output.append(line)
    return "".join(output)


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

    text = _PRIVATE_KEY_BLOCK.sub(_REDACTED, text)
    text = _TRUNCATED_PRIVATE_KEY_BEGIN.sub(_REDACTED, text)
    text = _redact_authorization_text(text)
    text = _COOKIE_LINE.sub(_replace_cookie_line, text)
    text = _COOKIE_INLINE.sub(_replace_cookie_line, text)
    text = _CLI_QUOTED.sub(_replace_secret_value, text)
    text = _TRUNCATED_CLI_QUOTED.sub(_replace_truncated_secret_value, text)
    text = _CLI_BARE.sub(_replace_secret_value, text)
    text = _QUOTED_KEY_VALUE.sub(_replace_secret_value, text)
    text = _redact_escaped_json_text(text)
    text = _URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:[REDACTED]@", text)
    text = _URL_PARAMETER.sub(_replace_url_parameter, text)
    text = _redact_bare_key_values(text)
    text = _TRUNCATED_QUOTED_KEY_VALUE.sub(_replace_truncated_secret_value, text)
    return _redact_percent_encoded_text(text)
