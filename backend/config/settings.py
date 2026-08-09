import ipaddress
import os
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured



BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} 必须为 true 或 false。")


def env_cookie_samesite(name, default="Lax"):
    raw = os.getenv(name, default).strip().lower()
    values = {"lax": "Lax", "strict": "Strict", "none": "None"}
    try:
        return values[raw]
    except KeyError as error:
        raise ImproperlyConfigured(f"{name} 必须为 Lax、Strict 或 None。") from error


def _validate_origin(value, *, setting_name, production):
    origin = str(value or "").strip().rstrip("/")
    if "*" in origin:
        raise ImproperlyConfigured(f"{setting_name} 禁止使用通配符：{value}")
    parsed = urlsplit(origin)
    if not parsed.scheme or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ImproperlyConfigured(f"{setting_name} 包含非法来源：{value}")
    if parsed.path not in {"", "/"}:
        raise ImproperlyConfigured(f"{setting_name} 只能配置 origin，不能包含路径：{value}")
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise ImproperlyConfigured(f"{setting_name} 包含非法端口：{value}") from error
    if parsed_port is not None and not (1 <= parsed_port <= 65535):
        raise ImproperlyConfigured(f"{setting_name} 包含非法端口：{value}")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.startswith("127."):
        if production:
            raise ImproperlyConfigured(f"{setting_name} 生产环境禁止 localhost 或回环地址：{value}")
    if production and parsed.scheme.lower() != "https":
        raise ImproperlyConfigured(f"{setting_name} 生产环境必须使用 HTTPS：{value}")
    return origin


def _validate_cookie_domain(value, *, setting_name, production):
    domain = str(value or "").strip()
    if not domain:
        return None
    if "://" in domain or "/" in domain or "*" in domain or any(character.isspace() for character in domain):
        raise ImproperlyConfigured(f"{setting_name} 必须是合法 Cookie 域名。")
    normalized = domain.lstrip(".").lower()
    if production and normalized in {"localhost", "127.0.0.1", "::1"}:
        raise ImproperlyConfigured(f"{setting_name} 生产环境禁止回环域名。")
    return domain


DEBUG = env_bool("DEBUG", False)

ANIME_JOURNAL_VERSION = os.getenv("ANIME_JOURNAL_VERSION", "0.0.0")
PLUGIN_ROOT = Path(os.getenv("PLUGIN_PACKAGE_ROOT") or os.getenv("PLUGIN_ROOT") or ("/app/runtime/plugins" if not DEBUG else BASE_DIR.parent / "plugins"))
PLUGIN_ASSET_SESSION_SECONDS = int(os.getenv("PLUGIN_ASSET_SESSION_SECONDS", "120"))
PLUGIN_PREVIEW_SESSION_SECONDS = int(os.getenv("PLUGIN_PREVIEW_SESSION_SECONDS", "600"))
PLUGIN_MAX_PACKAGE_BYTES = int(os.getenv("PLUGIN_MAX_PACKAGE_BYTES", str(25 * 1024 * 1024)))
PLUGIN_MAX_UNCOMPRESSED_BYTES = int(os.getenv("PLUGIN_MAX_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024)))
PLUGIN_MAX_FILES = int(os.getenv("PLUGIN_MAX_FILES", "1000"))
PLUGIN_MAX_COMPRESSION_RATIO = int(os.getenv("PLUGIN_MAX_COMPRESSION_RATIO", "100"))
PLUGIN_DRAFT_LIMIT = int(os.getenv("PLUGIN_DRAFT_LIMIT", "20"))
PLUGIN_UPLOADS_PER_HOUR = int(os.getenv("PLUGIN_UPLOADS_PER_HOUR", "12"))
PLUGIN_PACKAGE_GC_GRACE_SECONDS = int(os.getenv("PLUGIN_PACKAGE_GC_GRACE_SECONDS", "86400"))
CORE_PLUGIN_LOAD_ALLOWLIST = {"watch-history-importer"}
PLUGIN_LOAD_ALLOWLIST_CONFIGURED = env_list("PLUGIN_LOAD_ALLOWLIST")
PLUGIN_LOAD_ALLOWLIST_EFFECTIVE = set(PLUGIN_LOAD_ALLOWLIST_CONFIGURED or CORE_PLUGIN_LOAD_ALLOWLIST)
PLUGIN_KEEP_VERSIONS = int(os.getenv("PLUGIN_KEEP_VERSIONS", "2"))
PLUGIN_MIN_FREE_DISK_MB = int(os.getenv("PLUGIN_MIN_FREE_DISK_MB", "2048"))
INTEGRATION_PAIRING_CODE_TTL_SECONDS = int(os.getenv("INTEGRATION_PAIRING_CODE_TTL_SECONDS", "600"))
INTEGRATION_HMAC_TIMESTAMP_TOLERANCE_SECONDS = int(
    os.getenv("INTEGRATION_HMAC_TIMESTAMP_TOLERANCE_SECONDS", "300")
)
INTEGRATION_HMAC_NONCE_TTL_SECONDS = int(os.getenv("INTEGRATION_HMAC_NONCE_TTL_SECONDS", "660"))
INTEGRATION_ACTION_REQUEST_MAX_BYTES = int(
    os.getenv("INTEGRATION_ACTION_REQUEST_MAX_BYTES", str(256 * 1024))
)
INTEGRATION_PAIRING_REQUEST_MAX_BYTES = int(
    os.getenv("INTEGRATION_PAIRING_REQUEST_MAX_BYTES", str(8 * 1024))
)
INTEGRATION_ACK_REQUEST_MAX_BYTES = int(
    os.getenv("INTEGRATION_ACK_REQUEST_MAX_BYTES", str(16 * 1024))
)
INTEGRATION_EVENT_PAYLOAD_MAX_BYTES = int(
    os.getenv("INTEGRATION_EVENT_PAYLOAD_MAX_BYTES", str(64 * 1024))
)
INTEGRATION_EVENT_WAIT_DEFAULT_SECONDS = int(
    os.getenv("INTEGRATION_EVENT_WAIT_DEFAULT_SECONDS", "1")
)
INTEGRATION_EVENT_WAIT_MAX_SECONDS = int(os.getenv("INTEGRATION_EVENT_WAIT_MAX_SECONDS", "25"))
INTEGRATION_ACTION_RECEIPT_WAIT_SECONDS = float(
    os.getenv("INTEGRATION_ACTION_RECEIPT_WAIT_SECONDS", "5")
)
INTEGRATION_ACKED_EVENT_RETENTION_SECONDS = int(
    os.getenv("INTEGRATION_ACKED_EVENT_RETENTION_SECONDS", "86400")
)
INTEGRATION_UNACKED_EVENT_RETENTION_SECONDS = int(
    os.getenv("INTEGRATION_UNACKED_EVENT_RETENTION_SECONDS", "604800")
)
if PLUGIN_KEEP_VERSIONS < 2:
    raise ImproperlyConfigured("PLUGIN_KEEP_VERSIONS 至少为 2，以保留当前版本和 rollback 版本。")
if min(
    PLUGIN_MAX_PACKAGE_BYTES,
    PLUGIN_MAX_UNCOMPRESSED_BYTES,
    PLUGIN_MAX_FILES,
    PLUGIN_MAX_COMPRESSION_RATIO,
    PLUGIN_DRAFT_LIMIT,
    PLUGIN_UPLOADS_PER_HOUR,
) < 1:
    raise ImproperlyConfigured("插件上传与 Package 安全限制必须为正整数。")
if PLUGIN_PACKAGE_GC_GRACE_SECONDS < 0:
    raise ImproperlyConfigured("PLUGIN_PACKAGE_GC_GRACE_SECONDS 不能为负数。")
if min(
    INTEGRATION_PAIRING_CODE_TTL_SECONDS,
    INTEGRATION_HMAC_TIMESTAMP_TOLERANCE_SECONDS,
    INTEGRATION_HMAC_NONCE_TTL_SECONDS,
    INTEGRATION_ACTION_REQUEST_MAX_BYTES,
    INTEGRATION_PAIRING_REQUEST_MAX_BYTES,
    INTEGRATION_ACK_REQUEST_MAX_BYTES,
    INTEGRATION_EVENT_PAYLOAD_MAX_BYTES,
    INTEGRATION_ACKED_EVENT_RETENTION_SECONDS,
    INTEGRATION_UNACKED_EVENT_RETENTION_SECONDS,
) < 1:
    raise ImproperlyConfigured("Integration Protocol 安全限制必须为正数。")
if INTEGRATION_HMAC_NONCE_TTL_SECONDS < INTEGRATION_HMAC_TIMESTAMP_TOLERANCE_SECONDS * 2:
    raise ImproperlyConfigured("INTEGRATION_HMAC_NONCE_TTL_SECONDS 必须覆盖完整时间戳有效窗口。")
if not 0 <= INTEGRATION_EVENT_WAIT_DEFAULT_SECONDS <= INTEGRATION_EVENT_WAIT_MAX_SECONDS <= 25:
    raise ImproperlyConfigured("Integration event wait 必须满足 0 <= default <= max <= 25。")
if INTEGRATION_ACTION_RECEIPT_WAIT_SECONDS < 0:
    raise ImproperlyConfigured("INTEGRATION_ACTION_RECEIPT_WAIT_SECONDS 不能为负数。")


raw_secret_key = os.getenv("DJANGO_SECRET_KEY", "").strip()
UNSAFE_SECRET_KEYS = {
    "",
    "local-only-anime-journal-secret-key-change-this-before-production-2026",
    "replace-with-at-least-50-random-characters",
    "replace-with-a-random-secret-of-at-least-50-characters",
    "change-me",
}
if DEBUG:
    SECRET_KEY = raw_secret_key or "anime-journal-local-development-only-secret-key"
elif raw_secret_key in UNSAFE_SECRET_KEYS or len(raw_secret_key) < 50:
    raise ImproperlyConfigured("生产环境必须配置至少 50 个字符的随机 DJANGO_SECRET_KEY。")
else:
    SECRET_KEY = raw_secret_key
raw_credential_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
_credential_placeholders = {
    "replace-with-a-random-fernet-key-or-long-random-secret",
    "replace-with-a-random-fernet-key",
    "change-me",
    "your-fernet-key",
}
_development_credential_key = "a0DtqkhZwqytmU2lcF-2oUKmjlyqPIrJsU5O_T6d3Io="
if not DEBUG:
    if (
        not raw_credential_key
        or raw_credential_key.casefold() in {item.casefold() for item in _credential_placeholders}
        or raw_credential_key.casefold().startswith("replace-with-")
    ):
        raise ImproperlyConfigured("生产环境必须配置真实随机的 CREDENTIAL_ENCRYPTION_KEY。")
    try:
        Fernet(raw_credential_key.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as error:
        raise ImproperlyConfigured("CREDENTIAL_ENCRYPTION_KEY 必须是合法的 Fernet key。") from error
CREDENTIAL_ENCRYPTION_KEY = raw_credential_key or _development_credential_key
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("生产环境必须显式配置 ALLOWED_HOSTS。")
if not DEBUG:
    for configured_host in ALLOWED_HOSTS:
        normalized_host = configured_host.lstrip(".").lower()
        if (
            configured_host == "*"
            or "://" in configured_host
            or "/" in configured_host
            or normalized_host in {"localhost", "127.0.0.1", "::1"}
            or normalized_host.startswith("127.")
        ):
            raise ImproperlyConfigured(f"ALLOWED_HOSTS 包含不安全主机：{configured_host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "rest_framework_simplejwt.token_blacklist",
    "storages",
    "accounts",
    "journal",
    "site_config.apps.SiteConfig",
    "plugin_host",
    "integrations.apps.IntegrationsConfig",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.security_middleware.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "journal.admin_security_middleware.AdminSecondFactorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DEBUG and not DATABASE_URL:
    raise ImproperlyConfigured("生产环境必须配置 DATABASE_URL，并使用 PostgreSQL。")
DATABASE_SSL_REQUIRE = env_bool(
    "DATABASE_SSL_REQUIRE",
    not DEBUG and DATABASE_URL.startswith(("postgres://", "postgresql://")),
)
DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=DATABASE_SSL_REQUIRE,
    )
}
if not DEBUG and DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("生产环境只支持 PostgreSQL，SQLite 不允许启动。")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "").strip()
if not DEBUG:
    normalized_postgres_password = POSTGRES_PASSWORD.casefold()
    if (
        not POSTGRES_PASSWORD
        or normalized_postgres_password in {
            "change-me",
            "changeme",
            "password",
            "example-secret",
            "replace_with_strong_random_password",
            "replace-with-strong-random-password",
        }
        or normalized_postgres_password.startswith(("replace_", "replace-with-"))
    ):
        raise ImproperlyConfigured("生产环境必须配置真实随机的 POSTGRES_PASSWORD。")
    if str(DATABASES["default"].get("PASSWORD") or "") != POSTGRES_PASSWORD:
        raise ImproperlyConfigured("DATABASE_URL 中的数据库密码必须与 POSTGRES_PASSWORD 一致。")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

if DEBUG:
    STORAGES["default"] = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    MEDIA_URL = "/media/"
    MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", BASE_DIR / "media"))
else:
    # Production always uses the database-backed dynamic backend. It refuses
    # writes until a superuser completes R2 setup; it never falls back to disk.
    STORAGES["default"] = {"BACKEND": "site_config.media_storage.storage.StoragePoolStorage"}

MEDIA_LOCAL_STORAGE_ROOT = Path(os.getenv("MEDIA_LOCAL_STORAGE_ROOT", BASE_DIR / "managed-media")).resolve()

POSTER_UPLOAD_MAX_BYTES = int(os.getenv("POSTER_UPLOAD_MAX_BYTES", str(5 * 1024 * 1024)))
POSTER_STORAGE_QUOTA_BYTES = int(os.getenv("POSTER_STORAGE_QUOTA_BYTES", str(500 * 1024 * 1024)))
POSTER_UPLOAD_MAX_PIXELS = int(os.getenv("POSTER_UPLOAD_MAX_PIXELS", "24000000"))
POSTER_UPLOAD_MAX_WIDTH = int(os.getenv("POSTER_UPLOAD_MAX_WIDTH", "6000"))
POSTER_UPLOAD_MAX_HEIGHT = int(os.getenv("POSTER_UPLOAD_MAX_HEIGHT", "8000"))
AVATAR_UPLOAD_MAX_BYTES = int(os.getenv("AVATAR_UPLOAD_MAX_BYTES", str(2 * 1024 * 1024)))
AVATAR_UPLOAD_MAX_PIXELS = int(os.getenv("AVATAR_UPLOAD_MAX_PIXELS", "16000000"))
AVATAR_UPLOAD_MAX_WIDTH = int(os.getenv("AVATAR_UPLOAD_MAX_WIDTH", "4096"))
AVATAR_UPLOAD_MAX_HEIGHT = int(os.getenv("AVATAR_UPLOAD_MAX_HEIGHT", "4096"))
COLUMN_COVER_UPLOAD_MAX_BYTES = int(os.getenv("COLUMN_COVER_UPLOAD_MAX_BYTES", str(5 * 1024 * 1024)))
COLUMN_COVER_UPLOAD_MAX_PIXELS = int(os.getenv("COLUMN_COVER_UPLOAD_MAX_PIXELS", "24000000"))
COLUMN_COVER_UPLOAD_MAX_WIDTH = int(os.getenv("COLUMN_COVER_UPLOAD_MAX_WIDTH", "6000"))
COLUMN_COVER_UPLOAD_MAX_HEIGHT = int(os.getenv("COLUMN_COVER_UPLOAD_MAX_HEIGHT", "6000"))
IMPORT_FILE_MAX_BYTES = int(os.getenv("IMPORT_FILE_MAX_BYTES", str(2 * 1024 * 1024)))
IMPORT_MAX_RECORDS = int(os.getenv("IMPORT_MAX_RECORDS", "500"))
IMPORT_FIELD_MAX_LENGTH = int(os.getenv("IMPORT_FIELD_MAX_LENGTH", "10000"))
IMPORT_MAX_COLUMNS = int(os.getenv("IMPORT_MAX_COLUMNS", "32"))
IMPORT_MAX_LINE_LENGTH = int(os.getenv("IMPORT_MAX_LINE_LENGTH", "200000"))
IMPORT_MAX_NESTING_DEPTH = int(os.getenv("IMPORT_MAX_NESTING_DEPTH", "3"))
REGISTRATION_TOKEN_TTL_SECONDS = int(os.getenv("REGISTRATION_TOKEN_TTL_SECONDS", "3600"))
REGISTRATION_COMPLETION_TOKEN_TTL_SECONDS = int(os.getenv("REGISTRATION_COMPLETION_TOKEN_TTL_SECONDS", "900"))
EXTERNAL_SYNC_CONFIRMATION_MAX_AGE_SECONDS = int(
    os.getenv("EXTERNAL_SYNC_CONFIRMATION_MAX_AGE_SECONDS", "300")
)

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "config.rest_exceptions.exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "journal.authentication.SessionVersionJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 24,
    "DEFAULT_THROTTLE_CLASSES": (
        "journal.throttling.TrustedProxyAnonRateThrottle",
        "journal.throttling.TrustedProxyUserRateThrottle",
        "journal.throttling.TrustedProxyScopedRateThrottle",
        "journal.throttling.SecondaryScopedRateThrottle",
        "journal.throttling.HashedAccountRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("THROTTLE_ANON_RATE", "60/min"),
        "user": os.getenv("THROTTLE_USER_RATE", "300/min"),
        "login": os.getenv("THROTTLE_LOGIN_RATE", "5/min"),
        "login_ip": os.getenv("THROTTLE_LOGIN_IP_RATE", os.getenv("THROTTLE_LOGIN_RATE", "10/min")),
        "login_account": os.getenv("THROTTLE_LOGIN_ACCOUNT_RATE", "10/15min"),
        "login_combined": os.getenv("THROTTLE_LOGIN_COMBINED_RATE", "5/min"),
        "password_reset": os.getenv("THROTTLE_PASSWORD_RESET_RATE", "3/hour"),
        "password_reset_ip": os.getenv("THROTTLE_PASSWORD_RESET_IP_RATE", os.getenv("THROTTLE_PASSWORD_RESET_RATE", "5/hour")),
        "password_reset_account": os.getenv("THROTTLE_PASSWORD_RESET_ACCOUNT_RATE", "3/hour"),
        "password_reset_combined": os.getenv("THROTTLE_PASSWORD_RESET_COMBINED_RATE", "3/hour"),
        "register_request": os.getenv("THROTTLE_REGISTER_REQUEST_RATE", "3/hour"),
        "register_request_ip": os.getenv("THROTTLE_REGISTER_REQUEST_IP_RATE", "10/hour"),
        "register_request_account": os.getenv("THROTTLE_REGISTER_REQUEST_EMAIL_RATE", "3/hour"),
        "register_request_combined": os.getenv("THROTTLE_REGISTER_REQUEST_COMBINED_RATE", "3/hour"),
        "register_verify": os.getenv("THROTTLE_REGISTER_VERIFY_RATE", "10/hour"),
        "register_verify_ip": os.getenv("THROTTLE_REGISTER_VERIFY_IP_RATE", "10/hour"),
        "register_verify_account": os.getenv("THROTTLE_REGISTER_VERIFY_TOKEN_RATE", "10/hour"),
        "register_verify_combined": os.getenv("THROTTLE_REGISTER_VERIFY_COMBINED_RATE", "10/hour"),
        "register_complete": os.getenv("THROTTLE_REGISTER_COMPLETE_RATE", "10/hour"),
        "register_complete_ip": os.getenv("THROTTLE_REGISTER_COMPLETE_IP_RATE", "10/hour"),
        "register_complete_account": os.getenv("THROTTLE_REGISTER_COMPLETE_PENDING_RATE", "5/hour"),
        "register_complete_combined": os.getenv("THROTTLE_REGISTER_COMPLETE_COMBINED_RATE", "5/hour"),
        "two_factor": os.getenv("THROTTLE_TWO_FACTOR_RATE", "2/min"),
        "two_factor_ip": os.getenv("THROTTLE_TWO_FACTOR_IP_RATE", os.getenv("THROTTLE_TWO_FACTOR_RATE", "10/10min")),
        "two_factor_account": os.getenv("THROTTLE_TWO_FACTOR_ACCOUNT_RATE", "5/10min"),
        "two_factor_combined": os.getenv("THROTTLE_TWO_FACTOR_COMBINED_RATE", "3/5min"),
        "external_search": os.getenv("THROTTLE_EXTERNAL_SEARCH_RATE", "30/min"),
        "external_account": os.getenv("THROTTLE_EXTERNAL_ACCOUNT_RATE", "10/min"),
        "external_sync_preview": os.getenv("THROTTLE_EXTERNAL_SYNC_PREVIEW_RATE", "10/min"),
        "external_sync_apply": os.getenv("THROTTLE_EXTERNAL_SYNC_APPLY_RATE", "6/min"),
        "external_import_preview": os.getenv("THROTTLE_EXTERNAL_IMPORT_PREVIEW_RATE", "12/hour"),
        "external_import_apply": os.getenv("THROTTLE_EXTERNAL_IMPORT_APPLY_RATE", "10/hour"),
        "import": os.getenv("THROTTLE_IMPORT_RATE", "5/hour"),
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "AniMemo API",
    "DESCRIPTION": "AniMemo 番剧手账、观看记录、外部账号与插件平台 API。",
    "VERSION": ANIME_JOURNAL_VERSION,
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "SCHEMA_PATH_PREFIX": r"/api",
    "PREPROCESSING_HOOKS": ["config.openapi.exclude_dynamic_plugin_runtime"],
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "POSTPROCESSING_HOOKS": [
        "config.openapi.stabilize_operation_ids",
        "drf_spectacular.hooks.postprocess_schema_enums",
    ],
    "COMPONENT_SPLIT_REQUEST": True,
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "使用 access token：Authorization: Bearer <token>",
            },
            "refreshCookie": {
                "type": "apiKey",
                "in": "cookie",
                "name": os.getenv("REFRESH_COOKIE_NAME", "anime_journal_refresh"),
                "description": "refresh token 仅通过 HttpOnly Cookie 传递。",
            },
        },
        "schemas": {
            "ApiError": {
                "type": "object",
                "required": ["code", "detail"],
                "properties": {
                    "code": {"type": "string", "description": "稳定的 machine-readable 错误码。"},
                    "detail": {"type": "string", "description": "面向用户或调试的说明。"},
                    "fields": {
                        "type": "object",
                        "additionalProperties": {"type": "array", "items": {"type": "string"}},
                    },
                    "retry_after_seconds": {"type": "integer", "minimum": 1},
                },
            }
        },
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=10),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

_development_origins = (
    "http://localhost:4173,http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:5174,http://127.0.0.1:5174"
)
_raw_cors_origins = env_list("CORS_ALLOWED_ORIGINS", _development_origins if DEBUG else "")
_raw_csrf_origins = env_list("CSRF_TRUSTED_ORIGINS", _development_origins if DEBUG else "")
if not DEBUG and not _raw_cors_origins:
    raise ImproperlyConfigured("生产环境必须显式配置非空 CORS_ALLOWED_ORIGINS。")
if not DEBUG and not _raw_csrf_origins:
    raise ImproperlyConfigured("生产环境必须显式配置非空 CSRF_TRUSTED_ORIGINS。")
if "*" in _raw_cors_origins or "*" in _raw_csrf_origins:
    raise ImproperlyConfigured("CORS_ALLOWED_ORIGINS 和 CSRF_TRUSTED_ORIGINS 禁止使用通配符。")
CORS_ALLOWED_ORIGINS = [
    _validate_origin(value, setting_name="CORS_ALLOWED_ORIGINS", production=not DEBUG)
    for value in _raw_cors_origins
]
CSRF_TRUSTED_ORIGINS = [
    _validate_origin(value, setting_name="CSRF_TRUSTED_ORIGINS", production=not DEBUG)
    for value in _raw_csrf_origins
]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = False

REDIS_URL = os.getenv("REDIS_URL", "").strip()
AUTH_THROTTLE_CACHE_ALIAS = "default"
AUTH_THROTTLE_KEY_PREFIX = os.getenv("AUTH_THROTTLE_KEY_PREFIX", "anime-journal:auth-throttle")
AUTH_THROTTLE_FAIL_CLOSED = not DEBUG
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            "KEY_PREFIX": "anime-journal",
        }
    }
elif DEBUG or "test" in sys.argv:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "anime-journal-development",
        }
    }
else:
    raise ImproperlyConfigured("生产环境必须配置 REDIS_URL，确保安全限流在所有 Worker 间共享。")

TRUSTED_PROXY_IPS = env_list("TRUSTED_PROXY_IPS")
if not DEBUG and not TRUSTED_PROXY_IPS:
    raise ImproperlyConfigured("生产环境必须显式配置 TRUSTED_PROXY_IPS。")
for configured_proxy in TRUSTED_PROXY_IPS:
    try:
        configured_network = ipaddress.ip_network(configured_proxy, strict=False)
    except ValueError as error:
        raise ImproperlyConfigured(f"TRUSTED_PROXY_IPS 包含非法网段：{configured_proxy}") from error
    if configured_network.prefixlen == 0:
        raise ImproperlyConfigured("TRUSTED_PROXY_IPS 不能包含全网段地址。")
    if not DEBUG and str(configured_network) in {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}:
        raise ImproperlyConfigured(f"TRUSTED_PROXY_IPS 网段过于宽泛：{configured_network}")

REFRESH_COOKIE_NAME = os.getenv("REFRESH_COOKIE_NAME", "anime_journal_refresh")
REFRESH_COOKIE_PATH = os.getenv("REFRESH_COOKIE_PATH", "/api/")
if not REFRESH_COOKIE_PATH.startswith("/"):
    raise ImproperlyConfigured("REFRESH_COOKIE_PATH 必须以 / 开头。")
REFRESH_COOKIE_DOMAIN = _validate_cookie_domain(
    os.getenv("REFRESH_COOKIE_DOMAIN"),
    setting_name="REFRESH_COOKIE_DOMAIN",
    production=not DEBUG,
)
SESSION_COOKIE_DOMAIN = _validate_cookie_domain(
    os.getenv("SESSION_COOKIE_DOMAIN"),
    setting_name="SESSION_COOKIE_DOMAIN",
    production=not DEBUG,
)
CSRF_COOKIE_DOMAIN = _validate_cookie_domain(
    os.getenv("CSRF_COOKIE_DOMAIN"),
    setting_name="CSRF_COOKIE_DOMAIN",
    production=not DEBUG,
)
REFRESH_COOKIE_SAMESITE = env_cookie_samesite("REFRESH_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_SAMESITE = env_cookie_samesite("SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = env_cookie_samesite("CSRF_COOKIE_SAMESITE", "Lax")
REFRESH_COOKIE_SECURE = env_bool("REFRESH_COOKIE_SECURE", not DEBUG)
ADMIN_LOGIN_PATH = os.getenv("ADMIN_LOGIN_PATH", "/admin-login")

_raw_frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173" if DEBUG else "").strip()
if not _raw_frontend_url:
    raise ImproperlyConfigured("生产环境必须显式配置 FRONTEND_URL。")
FRONTEND_URL = _validate_origin(_raw_frontend_url, setting_name="FRONTEND_URL", production=not DEBUG)
BANGUMI_USER_AGENT = os.getenv("BANGUMI_USER_AGENT", "AniMemo/1.0 (+https://re-anime.cc)")
BANGUMI_IMAGE_PROXY_BASE_URL = os.getenv(
    "BANGUMI_IMAGE_PROXY_BASE_URL",
    "https://bgm-img-proxy.xhcytus100.workers.dev/",
).strip()
BANGUMI_ACCOUNT_INTEGRATION_ENABLED = env_bool("BANGUMI_ACCOUNT_INTEGRATION_ENABLED", True)
BANGUMI_OAUTH_CLIENT_ID = os.getenv("BANGUMI_OAUTH_CLIENT_ID", "").strip()
BANGUMI_OAUTH_CLIENT_SECRET = os.getenv("BANGUMI_OAUTH_CLIENT_SECRET", "").strip()
BANGUMI_OAUTH_REDIRECT_URI = os.getenv("BANGUMI_OAUTH_REDIRECT_URI", "").strip()
BANGUMI_IMPORT_MAX_ITEMS = int(os.getenv("BANGUMI_IMPORT_MAX_ITEMS", "1000"))
EXTERNAL_ACCOUNT_OAUTH_STATE_TTL_SECONDS = int(os.getenv("EXTERNAL_ACCOUNT_OAUTH_STATE_TTL_SECONDS", "600"))
EXTERNAL_IMPORT_PREVIEW_TTL_SECONDS = int(os.getenv("EXTERNAL_IMPORT_PREVIEW_TTL_SECONDS", "1200"))
EXTERNAL_IMPORT_APPLY_MAX_ITEMS = int(os.getenv("EXTERNAL_IMPORT_APPLY_MAX_ITEMS", "100"))
if min(
    BANGUMI_IMPORT_MAX_ITEMS,
    EXTERNAL_ACCOUNT_OAUTH_STATE_TTL_SECONDS,
    EXTERNAL_IMPORT_PREVIEW_TTL_SECONDS,
    EXTERNAL_IMPORT_APPLY_MAX_ITEMS,
) < 1:
    raise ImproperlyConfigured("外部账号连接与导入限制必须为正整数。")
if EXTERNAL_IMPORT_APPLY_MAX_ITEMS > BANGUMI_IMPORT_MAX_ITEMS:
    raise ImproperlyConfigured("EXTERNAL_IMPORT_APPLY_MAX_ITEMS 不能超过 provider 导入上限。")
if BANGUMI_OAUTH_REDIRECT_URI:
    _bangumi_redirect = urlsplit(BANGUMI_OAUTH_REDIRECT_URI)
    if (
        _bangumi_redirect.scheme not in ({"http", "https"} if DEBUG else {"https"})
        or not _bangumi_redirect.hostname
        or _bangumi_redirect.username
        or _bangumi_redirect.password
        or _bangumi_redirect.fragment
    ):
        raise ImproperlyConfigured("BANGUMI_OAUTH_REDIRECT_URI 必须是固定且安全的回调 URL。")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Anime Journal <noreply@example.com>")
TURNSTILE_SECRET = os.getenv("TURNSTILE_SECRET", "").strip()
# Local DEBUG defaults to no external verification; production defaults to
# fail-closed enforcement.
TURNSTILE_ENABLED = env_bool("TURNSTILE_ENABLED", not DEBUG)
if not DEBUG and TURNSTILE_ENABLED and not TURNSTILE_SECRET:
    raise ImproperlyConfigured("生产环境启用 Turnstile 时必须配置 TURNSTILE_SECRET。")
PASSWORD_RESET_TIMEOUT = 600
ADMIN_2FA_SESSION_MAX_AGE = int(os.getenv("ADMIN_2FA_SESSION_MAX_AGE", "28800"))
if ADMIN_2FA_SESSION_MAX_AGE <= 0:
    raise ImproperlyConfigured("ADMIN_2FA_SESSION_MAX_AGE 必须是正整数秒数。")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)
ALLOW_INSECURE_PRODUCTION_COOKIES = env_bool("ALLOW_INSECURE_PRODUCTION_COOKIES", False)
_cookie_settings = (
    ("SESSION_COOKIE_SAMESITE", SESSION_COOKIE_SAMESITE, SESSION_COOKIE_SECURE),
    ("CSRF_COOKIE_SAMESITE", CSRF_COOKIE_SAMESITE, CSRF_COOKIE_SECURE),
    ("REFRESH_COOKIE_SAMESITE", REFRESH_COOKIE_SAMESITE, REFRESH_COOKIE_SECURE),
)
for cookie_setting_name, same_site, secure in _cookie_settings:
    if same_site == "None" and not secure:
        raise ImproperlyConfigured(f"{cookie_setting_name}=None 时对应 Cookie 必须启用 Secure。")
if not DEBUG and any(same_site == "None" for _name, same_site, _secure in _cookie_settings):
    if not all(same_site == "None" for _name, same_site, _secure in _cookie_settings):
        raise ImproperlyConfigured("跨站 Cookie 模式必须让 Session、CSRF 和 refresh 三者都使用 SameSite=None。")
if not DEBUG and not ALLOW_INSECURE_PRODUCTION_COOKIES:
    insecure_cookie_names = [name.replace("_SAMESITE", "_SECURE") for name, _same_site, secure in _cookie_settings if not secure]
    if insecure_cookie_names:
        raise ImproperlyConfigured(f"生产环境必须启用 Secure Cookie：{', '.join(insecure_cookie_names)}")
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)

# Explicit, report-only CSP baseline. Runtime plugin assets stay constrained to
# the same origin until a deployment opts into an enforced policy.
CSP_REPORT_ONLY = env_bool("CSP_REPORT_ONLY", False)
CSP_REPORT_URI = os.getenv("CSP_REPORT_URI", "").strip()
_csp = (
    "default-src 'self'; "
    "script-src 'self' 'sha256-xjQsrThiVsL5TEjVM6dTosT1AwZvcPBi17BPpLuouCM=' https://challenges.cloudflare.com; "
    "style-src 'self'; "
    "style-src-attr 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://api.bgm.tv wss://re-anime.cc; "
    "frame-src https://challenges.cloudflare.com; "
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)
if CSP_REPORT_URI:
    _csp += f"; report-uri {CSP_REPORT_URI}"
SECURE_CONTENT_SECURITY_POLICY = _csp
