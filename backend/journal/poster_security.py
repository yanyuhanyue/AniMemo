import ipaddress
from urllib.parse import urlsplit

from site_config.models import SiteSettings


class PosterUrlValidationError(ValueError):
    pass


def trusted_poster_hosts():
    values = SiteSettings.load().trusted_poster_hosts or []
    return {str(value).strip().lower().rstrip(".") for value in values if str(value).strip()}


def validate_poster_url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise PosterUrlValidationError("封面必须使用受信任域名的 HTTPS 地址。")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise PosterUrlValidationError("封面地址不能直接使用 IP 地址。")
    if hostname not in trusted_poster_hosts():
        raise PosterUrlValidationError("该图片域名不在管理员维护的可信白名单中。")
    return value
