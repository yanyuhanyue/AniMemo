"""Closed, secret-safe R2 S3 origin-prestate verification.

The release Candidate path deliberately exposes only three S3 read methods and
constructs the Cloudflare endpoint from the verified account and jurisdiction.
There is no REST, public-CDN, GitHub, ambient AWS credential, or endpoint
fallback in this module.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator, FormatChecker

from .candidate import CandidateContractError, canonical_json_bytes, sha256_bytes

REPOSITORY = "yanyuhanyue/AniMemo"
TARGET_VERSION = "v1.1.0"
TARGET_RC = "v1.1.0-rc.14"
R2_RECEIPT_SCHEMA = "animemo.r2-origin-prestate-receipt/v2"
R2_AUTH_METHOD = "R2_S3_OBJECT_READ_ONLY"
R2_AUTH_METHOD_ARGUMENT = "s3-object-read-only"
R2_ACCOUNT_ID_SHA256 = (
    "sha256:c5afddc36ea670626be71b625029128a3381d836807378da8eada702bef541e1"
)
R2_BUCKET = "animemo-release-mirror"
R2_RC14_PREFIX = "yanyuhanyue/AniMemo/releases/download/v1.1.0-rc.14/"
R2_RC14_EXPECTED_KEYS = (
    "animemo-v1.1.0-rc.14-portable.tar",
    "checksums.txt",
    "deployment-contract.json",
    "installer-materials.tar",
    "mirror-receipt.json",
    "release-manifest.json",
)
R2_JURISDICTIONS = frozenset({"default", "eu", "fedramp", "us"})

ACCESS_KEY_ENV = "ANIMEMO_R2_S3_ACCESS_KEY_ID"
SECRET_KEY_ENV = "ANIMEMO_R2_S3_SECRET_ACCESS_KEY"
SESSION_TOKEN_ENV = "ANIMEMO_R2_S3_SESSION_TOKEN"
ACCOUNT_ID_ENV = "ANIMEMO_R2_ACCOUNT_ID"
JURISDICTION_ENV = "ANIMEMO_R2_JURISDICTION"

MAX_LIST_PAGES = 64
MAX_RECORDED_OBJECTS = 1
MAX_METADATA_BYTES = 16 * 1024
MAX_KEYS_PER_PAGE = 1000
READ_METHOD_COUNT = 3
WRITE_METHOD_COUNT = 0

_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SENSITIVE_FIELD = re.compile(
    r"(?:authorization|cookie|set-cookie|x-amz-(?:credential|signature|security-token)|"
    r"awsaccesskeyid|access[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|"
    r"session[_-]?token|credentials?|signature|signed[_-]?(?:headers|url))",
    re.IGNORECASE,
)
_UNTRUSTED_DIAGNOSTIC_FIELD = re.compile(
    r"(?:environment|stdout|stderr|logs?|logger|traceback|request|response|body|"
    r"repr|exception|sdk|headers?)",
    re.IGNORECASE,
)
_SIGNED_QUERY = re.compile(
    r"(?i)([?&](?:X-Amz-Credential|X-Amz-Signature|X-Amz-Security-Token|"
    r"AWSAccessKeyId)=)[^&#\s]+"
)
_SENSITIVE_HEADER_LINE = re.compile(
    r"(?im)\b(Authorization|Cookie|Set-Cookie)\s*:\s*[^\r\n]+"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(Authorization|Cookie|Set-Cookie|X-Amz-Credential|X-Amz-Signature|"
    r"X-Amz-Security-Token|AWSAccessKeyId|aws_access_key_id|aws_secret_access_key|"
    r"aws_session_token|access_key|secret_key|session_token|Credential|Signature|"
    r"SignedHeaders)"
    r"\s*[:=]\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;}&]+)"
)


class R2S3ReadonlyApi(Protocol):
    """The complete production-facing capability surface: three reads only."""

    def list_objects_v2(
        self, *, continuation_token: str | None = None
    ) -> Mapping[str, object]: ...

    def head_object(self, *, key: str) -> Mapping[str, object]: ...

    def get_object(self, *, key: str) -> Mapping[str, object]: ...


class R2S3PrecheckError(CandidateContractError):
    """Stable R2 failure with an optional already-bounded diagnostic payload."""

    def __init__(
        self, code: str, *, safe_diagnostic: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(code)
        self.safe_diagnostic = dict(safe_diagnostic or {})


class _R2ObjectNotFound(Exception):
    """Internal control flow for the one expected HeadObject failure."""


@dataclass(frozen=True, repr=False)
class R2S3Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def __repr__(self) -> str:
        return "R2S3Credentials([REDACTED])"


def _raise(
    code: str, *, safe_diagnostic: Mapping[str, object] | None = None
) -> None:
    raise R2S3PrecheckError(code, safe_diagnostic=safe_diagnostic)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        _raise("R2_S3_RESPONSE_INVALID")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_r2_s3_endpoint(account_id: str, jurisdiction: str) -> tuple[str, str]:
    if _ACCOUNT_ID.fullmatch(account_id) is None:
        _raise("R2_S3_ACCOUNT_MISMATCH")
    if sha256_bytes(account_id.encode("ascii")) != R2_ACCOUNT_ID_SHA256:
        _raise("R2_S3_ACCOUNT_MISMATCH")
    if jurisdiction not in R2_JURISDICTIONS:
        _raise("R2_S3_ENDPOINT_INVALID")
    suffix = "" if jurisdiction == "default" else f".{jurisdiction}"
    host = f"{account_id}{suffix}.r2.cloudflarestorage.com"
    return host, f"https://{host}"


def _valid_credential(value: str) -> bool:
    return bool(value) and len(value) <= 4096 and all(
        0x21 <= ord(character) <= 0x7E for character in value
    )


def credentials_from_environment(
    environment: Mapping[str, str],
) -> R2S3Credentials:
    access_key = environment.get(ACCESS_KEY_ENV, "")
    secret_key = environment.get(SECRET_KEY_ENV, "")
    session_token = environment.get(SESSION_TOKEN_ENV, "") or None
    if not access_key or not secret_key:
        _raise("R2_S3_CREDENTIAL_MISSING")
    if not _valid_credential(access_key) or not _valid_credential(secret_key):
        _raise("R2_S3_AUTHENTICATION_FAILED")
    if session_token is not None and not _valid_credential(session_token):
        _raise("R2_S3_AUTHENTICATION_FAILED")
    return R2S3Credentials(access_key, secret_key, session_token)


def _secret_values(environment: Mapping[str, str] | None) -> tuple[str, ...]:
    values = environment or {}
    found = {
        values.get(name, "")
        for name in (ACCESS_KEY_ENV, SECRET_KEY_ENV, SESSION_TOKEN_ENV)
        if values.get(name, "")
    }
    return tuple(sorted(found, key=len, reverse=True))


def _sanitize_string(value: str, secrets: Sequence[str]) -> str:
    sanitized = _SENSITIVE_HEADER_LINE.sub(
        lambda match: f"{match.group(1)}: [REDACTED]", value
    )
    sanitized = _SIGNED_QUERY.sub(r"\1[REDACTED]", sanitized)
    sanitized = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", sanitized
    )
    for secret in secrets:
        sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def sanitize_r2_diagnostic(
    value: object, *, environment: Mapping[str, str] | None = None
) -> object:
    """Redact credentials and signed-request material before every output sink."""

    secrets = _secret_values(environment)

    def clean(item: object) -> object:
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for key, nested in item.items():
                safe_key = _sanitize_string(str(key), secrets)
                result[safe_key] = (
                    "[REDACTED]"
                    if _SENSITIVE_FIELD.search(str(key))
                    or _UNTRUSTED_DIAGNOSTIC_FIELD.search(str(key))
                    else clean(nested)
                )
            return result
        if isinstance(item, (list, tuple)):
            return [clean(nested) for nested in item]
        if isinstance(item, bytes):
            return "[BINARY_DIAGNOSTIC_REMOVED]"
        if isinstance(item, BaseException):
            return "[EXCEPTION_DIAGNOSTIC_REMOVED]"
        if isinstance(item, str):
            return _sanitize_string(item, secrets)
        if item is None or type(item) in {bool, int, float}:
            return item
        return _sanitize_string(str(item), secrets)

    return clean(value)


def _classify_client_error(error: BaseException) -> str:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return "R2_S3_RESPONSE_INVALID"
    error_data = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    code = str(error_data.get("Code", "")) if isinstance(error_data, Mapping) else ""
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    if code in {
        "InvalidAccessKeyId",
        "InvalidToken",
        "ExpiredToken",
        "SignatureDoesNotMatch",
        "TokenRefreshRequired",
    } or status == 401:
        return "R2_S3_AUTHENTICATION_FAILED"
    if code in {"RequestTimeTooSkewed", "RequestExpired"}:
        return "R2_S3_CLOCK_SKEW"
    if code in {"AccessDenied", "Forbidden", "Unauthorized"} or status == 403:
        return "R2_S3_PERMISSION_DENIED"
    if code in {"NoSuchBucket", "InvalidBucketName"}:
        return "R2_S3_BUCKET_MISMATCH"
    if code in {"AuthorizationHeaderMalformed", "PermanentRedirect"}:
        return "R2_S3_ENDPOINT_INVALID"
    return "R2_S3_RESPONSE_INVALID"


def _is_missing_object(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    error_data = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    code = str(error_data.get("Code", "")) if isinstance(error_data, Mapping) else ""
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


class Boto3R2ReadonlyClient:
    """A narrow wrapper that intentionally has no S3 write method."""

    __slots__ = ("__client",)

    def __init__(
        self,
        *,
        account_id: str,
        jurisdiction: str,
        credentials: R2S3Credentials,
    ) -> None:
        _, endpoint = build_r2_s3_endpoint(account_id, jurisdiction)
        failed = False
        try:
            import boto3
            from botocore.config import Config
            from botocore.session import Session as BotocoreSession

            botocore_session = BotocoreSession(
                session_vars={
                    "profile": (None, [], None, None),
                    "config_file": (None, None, os.devnull, None),
                    "credentials_file": (None, None, os.devnull, None),
                }
            )
            session = boto3.Session(
                botocore_session=botocore_session,
                aws_access_key_id=credentials.access_key_id,
                aws_secret_access_key=credentials.secret_access_key,
                aws_session_token=credentials.session_token,
                region_name="auto",
            )
            self.__client = session.client(
                "s3",
                endpoint_url=endpoint,
                region_name="auto",
                use_ssl=True,
                verify=True,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"max_attempts": 3, "mode": "standard"},
                    proxies={},
                    s3={"addressing_style": "path"},
                ),
            )
        except Exception:  # noqa: BLE001 - suppress all SDK/config detail
            failed = True
        if failed:
            _raise("R2_S3_RESPONSE_INVALID")

    @staticmethod
    def _bound_key(key: str) -> str:
        if key not in {R2_RC14_PREFIX + name for name in R2_RC14_EXPECTED_KEYS}:
            _raise("R2_S3_RESPONSE_INVALID")
        return key

    def list_objects_v2(
        self, *, continuation_token: str | None = None
    ) -> Mapping[str, object]:
        request: dict[str, object] = {
            "Bucket": R2_BUCKET,
            "Prefix": R2_RC14_PREFIX,
            "MaxKeys": MAX_KEYS_PER_PAGE,
        }
        if continuation_token is not None:
            request["ContinuationToken"] = continuation_token
        failure_code: str | None = None
        try:
            response = self.__client.list_objects_v2(**request)
        except Exception as error:  # noqa: BLE001 - normalize SDK exceptions
            failure_code = _classify_client_error(error)
        if failure_code is not None:
            _raise(failure_code)
        if not isinstance(response, Mapping):
            _raise("R2_S3_RESPONSE_INVALID")
        return response

    def head_object(self, *, key: str) -> Mapping[str, object]:
        failure_code: str | None = None
        missing = False
        try:
            response = self.__client.head_object(
                Bucket=R2_BUCKET,
                Key=self._bound_key(key),
            )
        except Exception as error:  # noqa: BLE001 - normalize SDK exceptions
            if _is_missing_object(error):
                missing = True
            else:
                failure_code = _classify_client_error(error)
        if missing:
            raise _R2ObjectNotFound
        if failure_code is not None:
            _raise(failure_code)
        if not isinstance(response, Mapping):
            _raise("R2_S3_RESPONSE_INVALID")
        return response

    def get_object(self, *, key: str) -> Mapping[str, object]:
        failure_code: str | None = None
        try:
            response = self.__client.get_object(
                Bucket=R2_BUCKET,
                Key=self._bound_key(key),
            )
        except Exception as error:  # noqa: BLE001 - normalize SDK exceptions
            failure_code = _classify_client_error(error)
        if failure_code is not None:
            _raise(failure_code)
        if not isinstance(response, Mapping):
            _raise("R2_S3_RESPONSE_INVALID")
        return response


def _bounded_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _timestamp(value)
    text = str(value)
    if len(text.encode("utf-8")) > maximum or any(ord(char) < 0x20 for char in text):
        _raise("R2_S3_RESPONSE_INVALID")
    return text


def _inventory_item(value: Mapping[str, object]) -> dict[str, object]:
    key = _bounded_text(value.get("Key"), maximum=1024)
    if not key:
        _raise("R2_S3_RESPONSE_INVALID")
    size = value.get("Size", 0)
    if type(size) is not int or size < 0:
        _raise("R2_S3_RESPONSE_INVALID")
    item = {
        "key": key,
        "size": size,
        "etag": _bounded_text(value.get("ETag"), maximum=256),
        "last_modified": _bounded_text(value.get("LastModified"), maximum=128),
        "storage_class": _bounded_text(value.get("StorageClass"), maximum=64),
        "content_type": _bounded_text(value.get("ContentType"), maximum=256),
    }
    if len(canonical_json_bytes(item)) > MAX_METADATA_BYTES:
        _raise("R2_S3_RESPONSE_INVALID")
    return item


def _head_inventory(key: str, value: Mapping[str, object]) -> dict[str, object]:
    return _inventory_item(
        {
            "Key": key,
            "Size": value.get("ContentLength", 0),
            "ETag": value.get("ETag"),
            "LastModified": value.get("LastModified"),
            "StorageClass": value.get("StorageClass"),
            "ContentType": value.get("ContentType"),
        }
    )


def _validate_source_identity(source_sha: str, source_tree: str) -> None:
    if _GIT_SHA.fullmatch(source_sha) is None or _GIT_SHA.fullmatch(source_tree) is None:
        _raise("R2_S3_RESPONSE_INVALID")


def verify_r2_origin_empty(
    *,
    source_sha: str,
    source_tree: str,
    account_id: str,
    jurisdiction: str,
    credentials: R2S3Credentials,
    client: R2S3ReadonlyApi | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    _validate_source_identity(source_sha, source_tree)
    endpoint_host, _ = build_r2_s3_endpoint(account_id, jurisdiction)
    adapter = (
        client
        if client is not None
        else Boto3R2ReadonlyClient(
            account_id=account_id,
            jurisdiction=jurisdiction,
            credentials=credentials,
        )
    )
    started_at = _timestamp(clock())
    list_count = 0
    head_count = 0
    continuation: str | None = None
    seen_tokens: set[str] = set()

    while True:
        if list_count >= MAX_LIST_PAGES:
            _raise("R2_S3_RESPONSE_INVALID")
        failure_code: str | None = None
        try:
            page = adapter.list_objects_v2(continuation_token=continuation)
        except R2S3PrecheckError:
            raise
        except Exception as error:  # noqa: BLE001 - fake/SDK seam is intentionally closed
            failure_code = _classify_client_error(error)
        if failure_code is not None:
            _raise(failure_code)
        list_count += 1
        if not isinstance(page, Mapping):
            _raise("R2_S3_RESPONSE_INVALID")
        contents = page.get("Contents", [])
        common_prefixes = page.get("CommonPrefixes", [])
        if (
            not isinstance(contents, list)
            or not isinstance(common_prefixes, list)
            or len(contents) > MAX_KEYS_PER_PAGE
            or len(common_prefixes) > MAX_KEYS_PER_PAGE
            or len(contents) + len(common_prefixes) > MAX_KEYS_PER_PAGE
        ):
            _raise("R2_S3_RESPONSE_INVALID")
        first: Mapping[str, object] | None = None
        if contents:
            if not isinstance(contents[0], Mapping):
                _raise("R2_S3_RESPONSE_INVALID")
            first = contents[0]
        elif common_prefixes:
            prefix_value = common_prefixes[0]
            if not isinstance(prefix_value, Mapping):
                _raise("R2_S3_RESPONSE_INVALID")
            first = {"Key": prefix_value.get("Prefix"), "Size": 0}
        if first is not None:
            item = _inventory_item(first)
            _raise(
                "R2_S3_PREFIX_NON_EMPTY",
                safe_diagnostic={
                    "object_count_lower_bound": 1,
                    "object_inventory": [item][:MAX_RECORDED_OBJECTS],
                },
            )
        truncated = page.get("IsTruncated", False)
        if type(truncated) is not bool:
            _raise("R2_S3_RESPONSE_INVALID")
        if not truncated:
            break
        token = page.get("NextContinuationToken")
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > 4096
            or token in seen_tokens
        ):
            _raise("R2_S3_RESPONSE_INVALID")
        seen_tokens.add(token)
        continuation = token

    for name in R2_RC14_EXPECTED_KEYS:
        key = R2_RC14_PREFIX + name
        head_count += 1
        failure_code = None
        missing = False
        try:
            metadata = adapter.head_object(key=key)
        except _R2ObjectNotFound:
            missing = True
        except R2S3PrecheckError:
            raise
        except Exception as error:  # noqa: BLE001 - fake/SDK seam is intentionally closed
            if _is_missing_object(error):
                missing = True
            else:
                failure_code = _classify_client_error(error)
        if missing:
            continue
        if failure_code is not None:
            _raise(failure_code)
        if not isinstance(metadata, Mapping):
            _raise("R2_S3_RESPONSE_INVALID")
        item = _head_inventory(key, metadata)
        _raise(
            "R2_S3_PREFIX_NON_EMPTY",
            safe_diagnostic={
                "object_count_lower_bound": 1,
                "object_inventory": [item][:MAX_RECORDED_OBJECTS],
            },
        )

    completed_at = _timestamp(clock())
    receipt: dict[str, object] = {
        "schema": R2_RECEIPT_SCHEMA,
        "version": 2,
        "repository": REPOSITORY,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "target_version": TARGET_VERSION,
        "target_rc": TARGET_RC,
        "account_id": account_id,
        "bucket": R2_BUCKET,
        "jurisdiction": jurisdiction,
        "endpoint_host": endpoint_host,
        "auth_method": R2_AUTH_METHOD,
        "prefix": R2_RC14_PREFIX,
        "list_objects_v2_request_count": list_count,
        "head_object_request_count": head_count,
        "get_object_request_count": 0,
        "write_request_count": 0,
        "object_count": 0,
        "completion_marker_count": 0,
        "temporary_object_count": 0,
        "object_inventory": [],
        "started_at": started_at,
        "completed_at": completed_at,
        "result": "PROVEN_EMPTY",
        "error_code": None,
        "release_authority_granted": False,
        "publish_authorized": False,
        "receipt_digest": "",
    }
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    receipt["receipt_digest"] = sha256_bytes(canonical_json_bytes(unsigned))
    return validate_r2_origin_receipt(
        receipt,
        expected_source_sha=source_sha,
        expected_source_tree=source_tree,
    )


def verify_rc14_r2_origin_from_environment(
    *,
    source_sha: str,
    source_tree: str,
    auth_method: str,
    environment: Mapping[str, str] | None = None,
    client: R2S3ReadonlyApi | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    if auth_method != R2_AUTH_METHOD_ARGUMENT:
        _raise("R2_S3_AUTH_METHOD_INVALID")
    values = os.environ if environment is None else environment
    credentials = credentials_from_environment(values)
    account_id = values.get(ACCOUNT_ID_ENV, "")
    jurisdiction = values.get(JURISDICTION_ENV, "")
    return verify_r2_origin_empty(
        source_sha=source_sha,
        source_tree=source_tree,
        account_id=account_id,
        jurisdiction=jurisdiction,
        credentials=credentials,
        client=client,
        clock=clock,
    )


@lru_cache(maxsize=1)
def _receipt_validator() -> Draft202012Validator:
    schema_path = Path(__file__).with_name("r2-origin-prestate-receipt.schema.json")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _raise("R2_S3_RECEIPT_INVALID")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_r2_origin_receipt(
    value: object,
    *,
    expected_source_sha: str | None = None,
    expected_source_tree: str | None = None,
) -> dict[str, object]:
    errors = tuple(_receipt_validator().iter_errors(value))
    if errors or type(value) is not dict:
        _raise("R2_S3_RECEIPT_INVALID")
    receipt = dict(value)
    try:
        account_hash = sha256_bytes(receipt["account_id"].encode("ascii"))
        start = datetime.fromisoformat(receipt["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00"))
    except (AttributeError, UnicodeEncodeError, ValueError):
        _raise("R2_S3_RECEIPT_INVALID")
    try:
        expected_host, _ = build_r2_s3_endpoint(
            receipt["account_id"], receipt["jurisdiction"]
        )
    except R2S3PrecheckError:
        _raise("R2_S3_RECEIPT_INVALID")
    if (
        account_hash != R2_ACCOUNT_ID_SHA256
        or receipt["repository"] != REPOSITORY
        or receipt["target_version"] != TARGET_VERSION
        or receipt["target_rc"] != TARGET_RC
        or receipt["bucket"] != R2_BUCKET
        or receipt["prefix"] != R2_RC14_PREFIX
        or receipt["endpoint_host"] != expected_host
        or receipt["auth_method"] != R2_AUTH_METHOD
        or receipt["write_request_count"] != 0
        or receipt["object_count"] != 0
        or receipt["completion_marker_count"] != 0
        or receipt["temporary_object_count"] != 0
        or receipt["object_inventory"] != []
        or receipt["release_authority_granted"] is not False
        or receipt["publish_authorized"] is not False
        or receipt["result"] != "PROVEN_EMPTY"
        or receipt["error_code"] is not None
        or end < start
        or (expected_source_sha is not None and receipt["source_sha"] != expected_source_sha)
        or (expected_source_tree is not None and receipt["source_tree"] != expected_source_tree)
    ):
        _raise("R2_S3_RECEIPT_INVALID")
    unsigned = dict(receipt)
    digest = unsigned.pop("receipt_digest")
    if digest != sha256_bytes(canonical_json_bytes(unsigned)):
        _raise("R2_S3_RECEIPT_INVALID")
    return receipt


def r2_origin_receipt_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(validate_r2_origin_receipt(value)))


def write_r2_origin_receipt(path: Path, value: object) -> str:
    receipt = validate_r2_origin_receipt(value)
    encoded = canonical_json_bytes(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            _raise("R2_S3_RECEIPT_OUTPUT_EXISTS")
        return sha256_bytes(encoded)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
                _raise("R2_S3_RECEIPT_OUTPUT_EXISTS")
        if os.name != "nt":
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256_bytes(encoded)


__all__ = [
    "ACCESS_KEY_ENV",
    "ACCOUNT_ID_ENV",
    "JURISDICTION_ENV",
    "MAX_LIST_PAGES",
    "MAX_METADATA_BYTES",
    "MAX_RECORDED_OBJECTS",
    "R2_ACCOUNT_ID_SHA256",
    "R2_AUTH_METHOD",
    "R2_AUTH_METHOD_ARGUMENT",
    "R2_BUCKET",
    "R2_JURISDICTIONS",
    "R2_RC14_EXPECTED_KEYS",
    "R2_RC14_PREFIX",
    "R2_RECEIPT_SCHEMA",
    "READ_METHOD_COUNT",
    "SECRET_KEY_ENV",
    "SESSION_TOKEN_ENV",
    "WRITE_METHOD_COUNT",
    "Boto3R2ReadonlyClient",
    "R2S3Credentials",
    "R2S3PrecheckError",
    "R2S3ReadonlyApi",
    "build_r2_s3_endpoint",
    "credentials_from_environment",
    "r2_origin_receipt_digest",
    "sanitize_r2_diagnostic",
    "validate_r2_origin_receipt",
    "verify_r2_origin_empty",
    "verify_rc14_r2_origin_from_environment",
    "write_r2_origin_receipt",
]
