from __future__ import annotations

import base64
import dataclasses
import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote, urlsplit
from uuid import UUID

from .canonical import canonical_json_bytes, sha256_identity
from .instance import (
    DEFAULT_INSTANCE_NAME,
    InstanceName,
    InstanceNamespace,
    instance_namespace,
)
from .private_store import AtomicPrivateFile, PrivateStoreError

MANAGED_CONFIG_SCHEMA = "animemo.managed-config/v1"
_DEFAULT_NAMESPACE = instance_namespace()
MANAGED_CONFIG_PATH = _DEFAULT_NAMESPACE.managed_config_path
MANAGED_CONFIG_ROOT = MANAGED_CONFIG_PATH.parent
MANAGED_ENV_PATH = _DEFAULT_NAMESPACE.managed_env_path
MANAGED_ENV_ROOT = MANAGED_ENV_PATH.parent
STANDARD_DEPLOYMENT_PROFILE = "v1.1-instance-scoped"
MAX_MANAGED_CONFIG_BYTES = 1024 * 1024
MAX_MANAGED_ENV_BYTES = 1024 * 1024

SECRET_FIELDS = frozenset(
    {
        "application.credentialEncryptionKey",
        "application.djangoSecretKey",
        "database.password",
        "integrations.bangumiOAuthClientSecret",
        "integrations.resendApiKey",
        "redis.url",
    }
)
NON_SECRET_FIELDS = frozenset(
    {
        "application.mediaPublicOrigin",
        "application.trustedProxyIps",
        "configRevision",
        "instanceId",
        "database.name",
        "database.user",
        "deploymentProfile",
        "directAccess.allowHttp",
        "directAccess.allowNonLoopback",
        "directAccess.warningAcknowledged",
        "integrations.bangumiOAuthClientId",
        "listen.host",
        "listen.port",
        "publicOrigin",
        "schema",
        "trustedOrigins.allowedHosts",
        "trustedOrigins.cors",
        "trustedOrigins.csrf",
    }
)
DERIVED_ENV_FIELDS = frozenset(
    {
        "ALLOWED_HOSTS",
        "ANIMEMO_COMPOSE_PROJECT",
        "ANIMEMO_DATA_ROOT",
        "ANIMEMO_INSTANCE_NAME",
        "ANIMEMO_LISTEN_HOST",
        "ANIMEMO_LISTEN_PORT",
        "ANIMEMO_LOCATOR_DIGEST",
        "ANIMEMO_MANAGED_ENV_PATH",
        "ANIMEMO_UPDATER_RUNTIME_ROOT",
        "CORS_ALLOWED_ORIGINS",
        "CSRF_TRUSTED_ORIGINS",
        "DATABASE_URL",
        "MEDIA_LOCAL_STORAGE_ROOT",
        "SESSION_COOKIE_SECURE",
        "CSRF_COOKIE_SECURE",
        "REFRESH_COOKIE_SECURE",
    }
)
SECRET_ENV_FIELDS = frozenset(
    {
        "BANGUMI_OAUTH_CLIENT_SECRET",
        "CREDENTIAL_ENCRYPTION_KEY",
        "DATABASE_URL",
        "DJANGO_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "REDIS_URL",
        "RESEND_API_KEY",
    }
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "instanceId",
        "configRevision",
        "deploymentProfile",
        "listen",
        "publicOrigin",
        "directAccess",
        "trustedOrigins",
        "database",
        "redis",
        "application",
        "integrations",
    }
)
_NESTED_FIELDS = {
    "listen": frozenset({"host", "port"}),
    "directAccess": frozenset({"allowNonLoopback", "allowHttp", "warningAcknowledged"}),
    "trustedOrigins": frozenset({"allowedHosts", "cors", "csrf"}),
    "database": frozenset({"name", "user", "password"}),
    "redis": frozenset({"url"}),
    "application": frozenset(
        {
            "djangoSecretKey",
            "credentialEncryptionKey",
            "mediaPublicOrigin",
            "trustedProxyIps",
        }
    ),
    "integrations": frozenset(
        {"bangumiOAuthClientId", "bangumiOAuthClientSecret", "resendApiKey"}
    ),
}
_DATABASE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class ManagedConfigError(ValueError):
    """Stable and secret-safe managed configuration failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ListenConfig:
    host: str
    port: int

    @property
    def is_loopback(self) -> bool:
        return ipaddress.ip_address(self.host).is_loopback


@dataclass(frozen=True)
class DirectAccessConfig:
    allow_non_loopback: bool
    allow_http: bool
    warning_acknowledged: bool


@dataclass(frozen=True)
class TrustedOriginsConfig:
    allowed_hosts: tuple[str, ...]
    cors: tuple[str, ...]
    csrf: tuple[str, ...]


@dataclass(frozen=True)
class DatabaseConfig:
    name: str
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class RedisConfig:
    url: str = field(repr=False)


@dataclass(frozen=True)
class ApplicationConfig:
    django_secret_key: str = field(repr=False)
    credential_encryption_key: str = field(repr=False)
    media_public_origin: str | None = None
    trusted_proxy_ips: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationConfig:
    bangumi_oauth_client_id: str
    bangumi_oauth_client_secret: str = field(repr=False)
    resend_api_key: str = field(repr=False)


@dataclass(frozen=True)
class ManagedConfig:
    instance_id: str
    config_revision: str
    listen: ListenConfig
    public_origin: str
    direct_access: DirectAccessConfig
    trusted_origins: TrustedOriginsConfig
    database: DatabaseConfig = field(repr=False)
    redis: RedisConfig = field(repr=False)
    application: ApplicationConfig = field(repr=False)
    integrations: IntegrationConfig = field(repr=False)

    @property
    def schema(self) -> str:
        return MANAGED_CONFIG_SCHEMA

    @property
    def deployment_profile(self) -> str:
        return STANDARD_DEPLOYMENT_PROFILE

    def secret_safe_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "instanceId": self.instance_id,
            "configRevision": self.config_revision,
            "deploymentProfile": self.deployment_profile,
            "listen": {"host": self.listen.host, "port": self.listen.port},
            "publicOrigin": self.public_origin,
            "directAccess": {
                "allowNonLoopback": self.direct_access.allow_non_loopback,
                "allowHttp": self.direct_access.allow_http,
                "warningAcknowledged": self.direct_access.warning_acknowledged,
            },
            "secretStatus": {
                "application.credentialEncryptionKey": "configured",
                "application.djangoSecretKey": "configured",
                "database.password": "configured",
                "integrations.bangumiOAuthClientSecret": (
                    "configured"
                    if self.integrations.bangumi_oauth_client_secret
                    else "missing"
                ),
                "integrations.resendApiKey": (
                    "configured" if self.integrations.resend_api_key else "missing"
                ),
                "redis.url": "configured",
            },
        }


@dataclass(frozen=True)
class ConfigChangePlan:
    instance_id: str
    current_revision: str
    next_revision: str
    current_public_origin: str
    next_public_origin: str
    current_listen: ListenConfig
    next_listen: ListenConfig
    changed_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    plan_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "planIdentity": "animemo.managed-config-change/v1",
            "instanceId": self.instance_id,
            "currentRevision": self.current_revision,
            "nextRevision": self.next_revision,
            "currentPublicOrigin": self.current_public_origin,
            "nextPublicOrigin": self.next_public_origin,
            "currentListen": dataclasses.asdict(self.current_listen),
            "nextListen": dataclasses.asdict(self.next_listen),
            "changedFields": list(self.changed_fields),
            "warnings": list(self.warnings),
            "planDigest": self.plan_digest,
        }


def _fail(code: str) -> None:
    raise ManagedConfigError(code)


def _exact_object(
    value: object, fields: frozenset[str], code: str
) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != fields:
        _fail(code)
    return value


def _bounded_text(value: object, code: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        _fail(code)
    if not allow_empty and not value:
        _fail(code)
    if any(character in value for character in "\r\n"):
        _fail(code)
    return value


def _uuid(value: object, code: str) -> str:
    try:
        rendered = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        _fail(code)
    if rendered != value:
        _fail(code)
    return rendered


def _canonical_listen(value: object) -> ListenConfig:
    item = _exact_object(value, _NESTED_FIELDS["listen"], "CONFIG_LISTEN_INVALID")
    host = _bounded_text(item["host"], "CONFIG_LISTEN_INVALID")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        _fail("CONFIG_LISTEN_INVALID")
    if address.is_multicast or address.is_link_local:
        _fail("CONFIG_LISTEN_INVALID")
    port = item["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        _fail("CONFIG_LISTEN_INVALID")
    if address.compressed != host:
        _fail("CONFIG_LISTEN_INVALID")
    return ListenConfig(host=host, port=port)


def canonical_public_origin(value: object) -> str:
    origin = _bounded_text(value, "CONFIG_PUBLIC_ORIGIN_INVALID")
    if "*" in origin or any(character.isspace() for character in origin):
        _fail("CONFIG_PUBLIC_ORIGIN_INVALID")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        _fail("CONFIG_PUBLIC_ORIGIN_INVALID")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        _fail("CONFIG_PUBLIC_ORIGIN_INVALID")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        host = ipaddress.ip_address(hostname).compressed
        rendered_host = f"[{host}]" if ":" in host else host
    except ValueError:
        try:
            rendered_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            _fail("CONFIG_PUBLIC_ORIGIN_INVALID")
        if not _HOST.fullmatch(rendered_host):
            _fail("CONFIG_PUBLIC_ORIGIN_INVALID")
    if port is not None and not 1 <= port <= 65535:
        _fail("CONFIG_PUBLIC_ORIGIN_INVALID")
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    netloc = (
        rendered_host
        if port is None or port == default_port
        else f"{rendered_host}:{port}"
    )
    normalized = f"{parsed.scheme.lower()}://{netloc}"
    if origin != normalized:
        _fail("CONFIG_PUBLIC_ORIGIN_INVALID")
    return normalized


def _canonical_allowed_host(value: object) -> str:
    host = _bounded_text(value, "CONFIG_TRUSTED_ORIGINS_INVALID").lower().rstrip(".")
    if any(mark in host for mark in ("*", "/", "@")) or "://" in host:
        _fail("CONFIG_TRUSTED_ORIGINS_INVALID")
    try:
        return ipaddress.ip_address(host.strip("[]")).compressed
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            _fail("CONFIG_TRUSTED_ORIGINS_INVALID")
        if not _HOST.fullmatch(host):
            _fail("CONFIG_TRUSTED_ORIGINS_INVALID")
        return host


def _canonical_sequence(value: object, *, item_parser, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 64:
        _fail(code)
    parsed = tuple(item_parser(item) for item in value)
    if len(parsed) != len(set(parsed)):
        _fail(code)
    return tuple(sorted(parsed))


def _parse_direct(value: object) -> DirectAccessConfig:
    item = _exact_object(
        value, _NESTED_FIELDS["directAccess"], "CONFIG_DIRECT_ACCESS_INVALID"
    )
    values = tuple(item[field] for field in _NESTED_FIELDS["directAccess"])
    if any(not isinstance(candidate, bool) for candidate in values):
        _fail("CONFIG_DIRECT_ACCESS_INVALID")
    return DirectAccessConfig(
        allow_non_loopback=item["allowNonLoopback"],
        allow_http=item["allowHttp"],
        warning_acknowledged=item["warningAcknowledged"],
    )


def _parse_payload(payload: object) -> ManagedConfig:
    top = _exact_object(payload, _TOP_LEVEL_FIELDS, "CONFIG_SCHEMA_INVALID")
    if top["schema"] != MANAGED_CONFIG_SCHEMA:
        _fail("CONFIG_SCHEMA_UNSUPPORTED")
    if top["deploymentProfile"] != STANDARD_DEPLOYMENT_PROFILE:
        _fail("CONFIG_PROFILE_UNSUPPORTED")
    instance_id = _uuid(top["instanceId"], "CONFIG_INSTANCE_INVALID")
    revision = _uuid(top["configRevision"], "CONFIG_REVISION_INVALID")
    listen = _canonical_listen(top["listen"])
    public_origin = canonical_public_origin(top["publicOrigin"])
    direct = _parse_direct(top["directAccess"])
    if not listen.is_loopback and not direct.allow_non_loopback:
        _fail("CONFIG_DIRECT_ACCESS_REQUIRED")
    if public_origin.startswith("http://") and not direct.allow_http:
        _fail("CONFIG_HTTP_OPT_IN_REQUIRED")
    if direct.allow_non_loopback != (not listen.is_loopback):
        _fail("CONFIG_DIRECT_ACCESS_INVALID")
    if direct.allow_http != public_origin.startswith("http://"):
        _fail("CONFIG_DIRECT_ACCESS_INVALID")
    if direct.warning_acknowledged != (direct.allow_non_loopback or direct.allow_http):
        _fail("CONFIG_DIRECT_ACCESS_ACK_REQUIRED")

    trusted_raw = _exact_object(
        top["trustedOrigins"],
        _NESTED_FIELDS["trustedOrigins"],
        "CONFIG_TRUSTED_ORIGINS_INVALID",
    )
    trusted = TrustedOriginsConfig(
        allowed_hosts=_canonical_sequence(
            trusted_raw["allowedHosts"],
            item_parser=_canonical_allowed_host,
            code="CONFIG_TRUSTED_ORIGINS_INVALID",
        ),
        cors=_canonical_sequence(
            trusted_raw["cors"],
            item_parser=canonical_public_origin,
            code="CONFIG_TRUSTED_ORIGINS_INVALID",
        ),
        csrf=_canonical_sequence(
            trusted_raw["csrf"],
            item_parser=canonical_public_origin,
            code="CONFIG_TRUSTED_ORIGINS_INVALID",
        ),
    )
    if not direct.allow_http and any(
        origin.startswith("http://") for origin in (*trusted.cors, *trusted.csrf)
    ):
        _fail("CONFIG_TRUSTED_ORIGINS_INVALID")

    database_raw = _exact_object(
        top["database"], _NESTED_FIELDS["database"], "CONFIG_DATABASE_INVALID"
    )
    name = _bounded_text(database_raw["name"], "CONFIG_DATABASE_INVALID")
    user = _bounded_text(database_raw["user"], "CONFIG_DATABASE_INVALID")
    password = _bounded_text(database_raw["password"], "CONFIG_DATABASE_INVALID")
    if (
        not _DATABASE_IDENTIFIER.fullmatch(name)
        or not _DATABASE_IDENTIFIER.fullmatch(user)
        or len(password) < 32
    ):
        _fail("CONFIG_DATABASE_INVALID")
    database = DatabaseConfig(name=name, user=user, password=password)

    redis_raw = _exact_object(
        top["redis"], _NESTED_FIELDS["redis"], "CONFIG_REDIS_INVALID"
    )
    redis_url = _bounded_text(redis_raw["url"], "CONFIG_REDIS_INVALID")
    redis_parsed = urlsplit(redis_url)
    if redis_parsed.scheme not in {"redis", "rediss"} or not redis_parsed.hostname:
        _fail("CONFIG_REDIS_INVALID")
    redis = RedisConfig(redis_url)

    application_raw = _exact_object(
        top["application"],
        _NESTED_FIELDS["application"],
        "CONFIG_APPLICATION_INVALID",
    )
    django_secret = _bounded_text(
        application_raw["djangoSecretKey"], "CONFIG_APPLICATION_INVALID"
    )
    credential_key = _bounded_text(
        application_raw["credentialEncryptionKey"], "CONFIG_APPLICATION_INVALID"
    )
    if len(django_secret) < 50:
        _fail("CONFIG_APPLICATION_INVALID")
    try:
        decoded_key = base64.b64decode(
            credential_key.encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, ValueError):
        _fail("CONFIG_APPLICATION_INVALID")
    if (
        len(decoded_key) != 32
        or base64.urlsafe_b64encode(decoded_key).decode("ascii") != credential_key
    ):
        _fail("CONFIG_APPLICATION_INVALID")
    media_origin_raw = application_raw["mediaPublicOrigin"]
    media_origin = (
        None if media_origin_raw is None else canonical_public_origin(media_origin_raw)
    )
    if (
        media_origin is not None
        and media_origin.startswith("http://")
        and not direct.allow_http
    ):
        _fail("CONFIG_APPLICATION_INVALID")
    trusted_proxy_ips = _canonical_sequence(
        application_raw["trustedProxyIps"],
        item_parser=lambda value: _bounded_text(value, "CONFIG_APPLICATION_INVALID"),
        code="CONFIG_APPLICATION_INVALID",
    )
    for network in trusted_proxy_ips:
        try:
            ipaddress.ip_network(network, strict=False)
        except ValueError:
            _fail("CONFIG_APPLICATION_INVALID")
    application = ApplicationConfig(
        django_secret_key=django_secret,
        credential_encryption_key=credential_key,
        media_public_origin=media_origin,
        trusted_proxy_ips=trusted_proxy_ips,
    )

    integrations_raw = _exact_object(
        top["integrations"],
        _NESTED_FIELDS["integrations"],
        "CONFIG_INTEGRATIONS_INVALID",
    )
    integrations = IntegrationConfig(
        bangumi_oauth_client_id=_bounded_text(
            integrations_raw["bangumiOAuthClientId"],
            "CONFIG_INTEGRATIONS_INVALID",
            allow_empty=True,
        ),
        bangumi_oauth_client_secret=_bounded_text(
            integrations_raw["bangumiOAuthClientSecret"],
            "CONFIG_INTEGRATIONS_INVALID",
            allow_empty=True,
        ),
        resend_api_key=_bounded_text(
            integrations_raw["resendApiKey"],
            "CONFIG_INTEGRATIONS_INVALID",
            allow_empty=True,
        ),
    )
    return ManagedConfig(
        instance_id=instance_id,
        config_revision=revision,
        listen=listen,
        public_origin=public_origin,
        direct_access=direct,
        trusted_origins=trusted,
        database=database,
        redis=redis,
        application=application,
        integrations=integrations,
    )


def parse_managed_config(raw: bytes) -> ManagedConfig:
    if not isinstance(raw, bytes) or len(raw) > MAX_MANAGED_CONFIG_BYTES:
        _fail("CONFIG_SIZE_INVALID")

    def reject_constant(_: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        _fail("CONFIG_CONTENT_INVALID")
    return _parse_payload(payload)


def _config_payload(config: ManagedConfig) -> dict[str, object]:
    return {
        "schema": MANAGED_CONFIG_SCHEMA,
        "instanceId": config.instance_id,
        "configRevision": config.config_revision,
        "deploymentProfile": STANDARD_DEPLOYMENT_PROFILE,
        "listen": {"host": config.listen.host, "port": config.listen.port},
        "publicOrigin": config.public_origin,
        "directAccess": {
            "allowNonLoopback": config.direct_access.allow_non_loopback,
            "allowHttp": config.direct_access.allow_http,
            "warningAcknowledged": config.direct_access.warning_acknowledged,
        },
        "trustedOrigins": {
            "allowedHosts": list(config.trusted_origins.allowed_hosts),
            "cors": list(config.trusted_origins.cors),
            "csrf": list(config.trusted_origins.csrf),
        },
        "database": {
            "name": config.database.name,
            "user": config.database.user,
            "password": config.database.password,
        },
        "redis": {"url": config.redis.url},
        "application": {
            "djangoSecretKey": config.application.django_secret_key,
            "credentialEncryptionKey": config.application.credential_encryption_key,
            "mediaPublicOrigin": config.application.media_public_origin,
            "trustedProxyIps": list(config.application.trusted_proxy_ips),
        },
        "integrations": {
            "bangumiOAuthClientId": config.integrations.bangumi_oauth_client_id,
            "bangumiOAuthClientSecret": config.integrations.bangumi_oauth_client_secret,
            "resendApiKey": config.integrations.resend_api_key,
        },
    }


def canonical_managed_config_bytes(config: ManagedConfig) -> bytes:
    normalized = _parse_payload(_config_payload(config))
    payload = canonical_json_bytes(_config_payload(normalized)) + b"\n"
    if len(payload) > MAX_MANAGED_CONFIG_BYTES:
        _fail("CONFIG_SIZE_INVALID")
    return payload


def _origin_host(origin: str) -> str:
    return (urlsplit(origin).hostname or "").lower()


def _dotenv_value(value: str) -> str:
    if "\x00" in value or "\r" in value or "\n" in value:
        _fail("CONFIG_ENV_VALUE_INVALID")
    return json.dumps(value, ensure_ascii=False)


def derive_runtime_environment(
    config: ManagedConfig,
    *,
    namespace: InstanceNamespace | None = None,
    locator_digest: str | None = None,
) -> MappingProxyType[str, str]:
    selected = namespace or _DEFAULT_NAMESPACE
    public_host = _origin_host(config.public_origin)
    allowed_hosts = tuple(
        dict.fromkeys((public_host, *config.trusted_origins.allowed_hosts))
    )
    cors = tuple(dict.fromkeys((config.public_origin, *config.trusted_origins.cors)))
    csrf = tuple(dict.fromkeys((config.public_origin, *config.trusted_origins.csrf)))
    secure_cookies = not config.direct_access.allow_http
    database_url = (
        "postgresql://"
        f"{quote(config.database.user, safe='')}:{quote(config.database.password, safe='')}"
        "@postgres:5432/"
        f"{quote(config.database.name, safe='')}"
    )
    values = {
        "ALLOWED_HOSTS": ",".join(allowed_hosts),
        "ALLOW_INSECURE_PRODUCTION_COOKIES": str(
            config.direct_access.allow_http
        ).lower(),
        "ANIMEMO_CONFIG_REVISION": config.config_revision,
        "ANIMEMO_COMPOSE_PROJECT": selected.compose_project,
        "ANIMEMO_DATA_ROOT": str(selected.data_root),
        "ANIMEMO_DEPLOYMENT_PROFILE": STANDARD_DEPLOYMENT_PROFILE,
        "ANIMEMO_INSTANCE_NAME": str(selected.name),
        "ANIMEMO_INSTANCE_ID": config.instance_id,
        "ANIMEMO_LISTEN_HOST": config.listen.host,
        "ANIMEMO_LISTEN_PORT": str(config.listen.port),
        "ANIMEMO_MANAGED_ENV_PATH": str(selected.managed_env_path),
        "ANIMEMO_MANAGED_CONFIG_SCHEMA": MANAGED_CONFIG_SCHEMA,
        "ANIMEMO_PUBLIC_ORIGIN": config.public_origin,
        "ANIMEMO_UPDATER_RUNTIME_ROOT": str(selected.updater_runtime_root),
        "CORS_ALLOWED_ORIGINS": ",".join(cors),
        "CREDENTIAL_ENCRYPTION_KEY": config.application.credential_encryption_key,
        "CSRF_COOKIE_SECURE": str(secure_cookies).lower(),
        "CSRF_TRUSTED_ORIGINS": ",".join(csrf),
        "DATABASE_SSL_REQUIRE": "false",
        "DATABASE_URL": database_url,
        "DEBUG": "false",
        "DJANGO_SECRET_KEY": config.application.django_secret_key,
        "MEDIA_LOCAL_STORAGE_ROOT": str(selected.data_root / "media"),
        "POSTGRES_DB": config.database.name,
        "POSTGRES_PASSWORD": config.database.password,
        "POSTGRES_USER": config.database.user,
        "REDIS_URL": config.redis.url,
        "REFRESH_COOKIE_SECURE": str(secure_cookies).lower(),
        "SESSION_COOKIE_SECURE": str(secure_cookies).lower(),
        "TRUSTED_PROXY_IPS": ",".join(config.application.trusted_proxy_ips),
    }
    if locator_digest is not None:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", locator_digest) is None:
            _fail("CONFIG_LOCATOR_DIGEST_INVALID")
        values["ANIMEMO_LOCATOR_DIGEST"] = locator_digest
    if config.application.media_public_origin is not None:
        values["ANIMEMO_MEDIA_PUBLIC_ORIGIN"] = config.application.media_public_origin
    if config.integrations.bangumi_oauth_client_id:
        values["BANGUMI_OAUTH_CLIENT_ID"] = config.integrations.bangumi_oauth_client_id
    if config.integrations.bangumi_oauth_client_secret:
        values["BANGUMI_OAUTH_CLIENT_SECRET"] = (
            config.integrations.bangumi_oauth_client_secret
        )
    if config.integrations.resend_api_key:
        values["RESEND_API_KEY"] = config.integrations.resend_api_key
    return MappingProxyType(dict(sorted(values.items())))


def canonical_managed_env_bytes(
    config: ManagedConfig,
    *,
    namespace: InstanceNamespace | None = None,
    locator_digest: str | None = None,
) -> bytes:
    environment = derive_runtime_environment(
        config, namespace=namespace, locator_digest=locator_digest
    )
    payload = "".join(
        f"{key}={_dotenv_value(value)}\n" for key, value in environment.items()
    ).encode("utf-8")
    if len(payload) > MAX_MANAGED_ENV_BYTES:
        _fail("CONFIG_ENV_SIZE_INVALID")
    return payload


def plan_config_change(
    current: ManagedConfig,
    *,
    next_revision: str,
    public_origin: str | None = None,
    listen: ListenConfig | None = None,
    direct_access: DirectAccessConfig | None = None,
) -> tuple[ConfigChangePlan, ManagedConfig]:
    revision = _uuid(next_revision, "CONFIG_REVISION_INVALID")
    if revision == current.config_revision:
        _fail("CONFIG_REVISION_NOT_CHANGED")
    next_config = dataclasses.replace(
        current,
        config_revision=revision,
        public_origin=(
            current.public_origin
            if public_origin is None
            else canonical_public_origin(public_origin)
        ),
        listen=current.listen if listen is None else listen,
        direct_access=(
            current.direct_access if direct_access is None else direct_access
        ),
    )
    next_config = _parse_payload(_config_payload(next_config))
    changes: list[str] = []
    if next_config.public_origin != current.public_origin:
        changes.append("publicOrigin")
    if next_config.listen != current.listen:
        changes.append("listen")
    if next_config.direct_access != current.direct_access:
        changes.append("directAccess")
    warnings: list[str] = []
    if not next_config.listen.is_loopback:
        warnings.append("DIRECT_NETWORK_EXPOSURE")
    if next_config.public_origin.startswith("http://"):
        warnings.extend(("HTTP_WITHOUT_TLS", "SECURE_COOKIES_DISABLED"))
    body = {
        "planIdentity": "animemo.managed-config-change/v1",
        "instanceId": current.instance_id,
        "currentRevision": current.config_revision,
        "nextRevision": revision,
        "currentPublicOrigin": current.public_origin,
        "nextPublicOrigin": next_config.public_origin,
        "currentListen": dataclasses.asdict(current.listen),
        "nextListen": dataclasses.asdict(next_config.listen),
        "changedFields": changes,
        "warnings": warnings,
    }
    plan = ConfigChangePlan(
        instance_id=current.instance_id,
        current_revision=current.config_revision,
        next_revision=revision,
        current_public_origin=current.public_origin,
        next_public_origin=next_config.public_origin,
        current_listen=current.listen,
        next_listen=next_config.listen,
        changed_fields=tuple(changes),
        warnings=tuple(warnings),
        plan_digest=sha256_identity(canonical_json_bytes(body)),
    )
    return plan, next_config


class LocalManagedConfigStore:
    """Atomic protected authority plus its re-creatable runtime env adapter."""

    def __init__(
        self,
        *,
        instance_name: InstanceName | str = DEFAULT_INSTANCE_NAME,
        config_root: Path | None = None,
        runtime_root: Path | None = None,
        create_runtime_root: bool = False,
    ) -> None:
        self.namespace = instance_namespace(instance_name)
        selected_config_root = config_root or Path(str(self.namespace.managed_config_path.parent))
        selected_runtime_root = runtime_root or Path(str(self.namespace.updater_runtime_root))
        self._authority = AtomicPrivateFile(
            selected_config_root, self.namespace.managed_config_path.name
        )
        self._runtime = AtomicPrivateFile(
            selected_runtime_root,
            self.namespace.managed_env_path.name,
            create_parents=create_runtime_root,
            directory_mode=0o750,
        )

    @property
    def authority_path(self) -> Path:
        return self._authority.path

    @property
    def runtime_env_path(self) -> Path:
        return self._runtime.path

    def read(self) -> ManagedConfig:
        try:
            return parse_managed_config(
                self._authority.read(limit=MAX_MANAGED_CONFIG_BYTES)
            )
        except PrivateStoreError as error:
            raise ManagedConfigError(error.code) from None

    def write(
        self,
        config: ManagedConfig,
        *,
        expected_revision: str | None,
        must_not_exist: bool = False,
    ) -> None:
        if not must_not_exist:
            current = self.read()
            if (
                expected_revision is None
                or current.config_revision != expected_revision
            ):
                _fail("CONFIG_STALE")
        elif expected_revision is not None:
            _fail("CONFIG_EXPECTATION_INVALID")
        try:
            self._authority.write(
                canonical_managed_config_bytes(config),
                must_not_exist=must_not_exist,
            )
        except PrivateStoreError as error:
            raise ManagedConfigError(error.code) from None

    def rebuild_runtime_env(
        self,
        *,
        locator_digest: str,
        expected_revision: str | None = None,
    ) -> Path:
        config = self.read()
        if (
            expected_revision is not None
            and config.config_revision != expected_revision
        ):
            _fail("CONFIG_STALE")
        try:
            self._runtime.write(
                canonical_managed_env_bytes(
                    config,
                    namespace=self.namespace,
                    locator_digest=locator_digest,
                )
            )
        except PrivateStoreError as error:
            raise ManagedConfigError(error.code) from None
        return self.runtime_env_path


__all__ = [
    "DERIVED_ENV_FIELDS",
    "MANAGED_CONFIG_PATH",
    "MANAGED_CONFIG_SCHEMA",
    "MANAGED_ENV_PATH",
    "NON_SECRET_FIELDS",
    "SECRET_ENV_FIELDS",
    "SECRET_FIELDS",
    "ApplicationConfig",
    "ConfigChangePlan",
    "DatabaseConfig",
    "DirectAccessConfig",
    "IntegrationConfig",
    "ListenConfig",
    "LocalManagedConfigStore",
    "ManagedConfig",
    "ManagedConfigError",
    "RedisConfig",
    "TrustedOriginsConfig",
    "canonical_managed_config_bytes",
    "canonical_managed_env_bytes",
    "canonical_public_origin",
    "derive_runtime_environment",
    "parse_managed_config",
    "plan_config_change",
]
