from urllib.parse import urlparse

from django.conf import settings


def normalize_external_id(value):
    normalized = str(value or "").strip()
    if (
        not normalized.isascii()
        or not normalized.isdigit()
        or len(normalized) > 20
        or int(normalized) <= 0
    ):
        raise ValueError("invalid_external_id")
    return str(int(normalized))


def canonical_url(external_id):
    return f"https://bgm.tv/subject/{normalize_external_id(external_id)}"


def image_url(value, *, resize=None):
    url = _https_url(value)
    if len(url) > 1000:
        return ""
    parsed = urlparse(url)
    if parsed.hostname != "lain.bgm.tv" or not parsed.path or parsed.username or parsed.password:
        return ""
    proxy_base = str(getattr(settings, "BANGUMI_IMAGE_PROXY_BASE_URL", "") or "").strip()
    proxy = urlparse(proxy_base)
    if not proxy_base or proxy.scheme != "https" or not proxy.hostname or proxy.username or proxy.password:
        return url
    resize_path = f"r/{resize}/" if resize else ""
    proxied_url = f"{proxy_base.rstrip('/')}/{resize_path}{parsed.path.lstrip('/')}"
    return proxied_url if len(proxied_url) <= 1000 else ""


def normalize_subject(item, *, persons=None):
    external_id = normalize_external_id(item.get("id"))
    images = item.get("images") if isinstance(item.get("images"), dict) else {}
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
    source_poster = images.get("large") or images.get("common") or images.get("medium")
    episodes = item.get("eps") if item.get("eps") is not None else item.get("total_episodes")
    if isinstance(episodes, list):
        episodes = len(episodes) or None
    elif episodes in (None, "", 0, "0"):
        episodes = None
    else:
        try:
            episodes = int(episodes)
        except (TypeError, ValueError):
            episodes = None
    if episodes is not None and not 1 <= episodes <= 999999:
        episodes = None
    studio = _person_studios(persons or []) or _studio(item)
    return {
        "provider": "bangumi",
        "external_id": external_id,
        "title": _bounded_text(item.get("name_cn") or item.get("name"), 500),
        "japanese_title": _bounded_text(item.get("name"), 500),
        "summary": _bounded_text(item.get("summary"), 5000),
        "episodes": episodes,
        "air_date": _bounded_text(item.get("date") or item.get("air_date"), 32),
        "studio": _bounded_text(studio, 500),
        "tags": _tags(item),
        "score": _score(rating.get("score")),
        "poster_url": image_url(source_poster),
        "thumbnail_url": image_url(source_poster, resize=100),
        "canonical_url": canonical_url(external_id),
    }


def _https_url(value):
    url = str(value or "").strip()
    return f"https://{url[7:]}" if url.startswith("http://") else url


def _bounded_text(value, max_length):
    return str(value or "").strip()[:max_length]


def _score(value):
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 10 else None


def _text_values(value):
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_text_values(item))
        return values
    if isinstance(value, dict):
        for key in ("v", "value", "name", "title"):
            if key in value:
                return _text_values(value[key])
    return []


def _studio(item):
    infobox = [info for info in item.get("infobox") or [] if isinstance(info, dict)]
    for accepted_keys in (
        {"动画制作", "動畫製作", "アニメーション制作"},
        {"制作公司", "製作公司"},
        {"制作", "製作"},
    ):
        for info in infobox:
            if str(info.get("key") or "").strip() not in accepted_keys:
                continue
            values = _text_values(info.get("value"))
            if values:
                return " / ".join(dict.fromkeys(values))
    return ""


def _person_studios(persons):
    accepted = {"动画制作", "動畫製作", "アニメーション制作"}
    studios = []
    for person in persons:
        if not isinstance(person, dict):
            continue
        relation = str(person.get("relation") or "").strip()
        name = str(person.get("name") or "").strip()
        if relation in accepted and name and name not in studios:
            studios.append(name)
    return " / ".join(studios)


def _tags(item):
    tags = []
    for tag in item.get("tags") or []:
        for value in _text_values(tag):
            value = _bounded_text(value, 100)
            if value not in tags:
                tags.append(value)
            if len(tags) == 8:
                return tags
    return tags
