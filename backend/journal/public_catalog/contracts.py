"""Small wire and token contracts for live, bounded public catalogue reads."""
import hashlib
import json
import re
from dataclasses import dataclass

from django.core import signing

SCHEMA = "animemo.public-catalog/v1"
SUCCESS_BYTES = 524_288
ERROR_BYTES = 16_384
TOKEN_BYTES = 4_096
TOKEN_SECONDS = 15 * 60
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
DEFAULT_PART_SIZE = 4_096
MAX_PART_SIZE = 16_384
COLLATION = "animemo_public_zh_cn_v1"
SORTS = frozenset({"date-desc", "date-asc", "score-desc", "score-asc", "id-asc"})
QUICK_TAGS = {
    "all": (), "yuri": ("真百", "轻百"), "daily": ("萌系", "日常"),
    "school": ("搞笑", "校园"), "original": ("原创", "治愈"),
    "special": ("剧场版", "OVA", "泡面番"),
}
FIELDS = ("description", "review", "tags", "tag_colors")
JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
PUBLIC_FIELDS = (
    "id", "title", "japanese_title", "airing_period", "studio", "episodes",
    "description", "poster_url", "poster", "baike_url", "tags", "tag_colors",
    "personal_score", "watch_status", "watch_status_display", "review",
    "visibility", "watch_history_count", "created_at", "updated_at",
)
_SALT = "animemo.public-catalog.live.v1"


class CatalogError(ValueError):
    def __init__(self, code="invalid_request", status=400):
        super().__init__(code)
        self.code, self.status = code, status


def bounded_text(value, maximum=4096):
    if not isinstance(value, str):
        raise CatalogError()
    try:
        if len(value.encode("utf-8")) > maximum:
            raise CatalogError()
    except UnicodeError:
        raise CatalogError() from None
    return value


def positive_int(value, *, default, maximum):
    if value is None:
        return default
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,5}", value):
        raise CatalogError()
    number = int(value)
    if number > maximum:
        raise CatalogError()
    return number


def digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def issue_token(purpose, context, position):
    token = signing.dumps({"v": 1, "p": purpose, "c": context, "k": position}, salt=_SALT, compress=False)
    if len(token.encode("utf-8")) > TOKEN_BYTES:
        raise CatalogError("public_catalog_budget_exceeded", 500)
    return token


def read_token(token, purpose, context):
    try:
        bounded_text(token, TOKEN_BYTES)
        value = signing.loads(token, salt=_SALT, max_age=TOKEN_SECONDS)
    except signing.SignatureExpired:
        raise CatalogError("public_catalog_cursor_expired", 410) from None
    except (signing.BadSignature, ValueError, TypeError, UnicodeError):
        raise CatalogError("public_catalog_cursor_invalid", 400) from None
    if not isinstance(value, dict) or set(value) != {"v", "p", "c", "k"} or value["v"] != 1:
        raise CatalogError("public_catalog_cursor_invalid", 400)
    if value["p"] != purpose or value["c"] != context:
        raise CatalogError("public_catalog_cursor_mismatch", 400)
    if not isinstance(value["k"], dict):
        raise CatalogError("public_catalog_cursor_invalid", 400)
    return value["k"]


def check_params(params, allowed):
    if set(params) - set(allowed):
        raise CatalogError()
    if hasattr(params, "getlist") and any(len(params.getlist(key)) != 1 for key in params):
        raise CatalogError()


@dataclass(frozen=True)
class PublicQuery:
    search: str = ""
    tag: str = ""
    tag_ref: str = ""
    status: str = ""
    year: str = ""
    year_ref: str = ""
    quick: str = "all"
    sort: str = "date-desc"

    @property
    def identity(self):
        return digest(vars(self))

    @classmethod
    def parse(cls, params):
        fields = {}
        for name in ("search", "tag", "tag_ref", "status", "year", "year_ref", "quick", "sort"):
            default = "all" if name == "quick" else "date-desc" if name == "sort" else ""
            value = bounded_text(params.get(name, default))
            if name in {"search", "status", "quick", "sort"}:
                value = value.strip(JS_WHITESPACE)
            if name in {"tag", "year", "status"} and value == "all":
                value = ""
            fields[name] = value
        if fields["sort"] not in SORTS or fields["quick"] not in QUICK_TAGS:
            raise CatalogError()
        if fields["status"] not in {"", "completed", "watching", "planned", "on_hold", "dropped"}:
            raise CatalogError()
        if (fields["tag"] and fields["tag_ref"]) or (fields["year"] and fields["year_ref"]):
            raise CatalogError()
        return cls(**fields)
