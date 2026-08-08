import logging
from urllib.parse import quote, urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView


logger = logging.getLogger(__name__)


def _https_url(value):
    url = str(value or "")
    return f"https://{url[7:]}" if url.startswith("http://") else url


def _bangumi_image_proxy(value, resize=None):
    url = _https_url(value)
    parsed = urlparse(url)
    if parsed.netloc.lower() != "lain.bgm.tv" or not parsed.path:
        return url
    resize_path = f"r/{resize}/" if resize else ""
    return f"https://bgm-img-proxy.xhcytus100.workers.dev/{resize_path}{parsed.path.lstrip('/')}"


def _bangumi_text_values(value):
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_bangumi_text_values(item))
        return values
    if isinstance(value, dict):
        for key in ("v", "value", "name", "title"):
            if key in value:
                return _bangumi_text_values(value[key])
    return []


def _bangumi_studio(item):
    infobox = [info for info in item.get("infobox") or [] if isinstance(info, dict)]
    key_priority = (
        {"动画制作", "動畫製作", "アニメーション制作"},
        {"制作公司", "製作公司"},
        {"制作", "製作"},
    )
    for accepted_keys in key_priority:
        for info in infobox:
            if str(info.get("key") or "").strip() not in accepted_keys:
                continue
            values = _bangumi_text_values(info.get("value"))
            if values:
                return " / ".join(dict.fromkeys(values))
    return ""


def _bangumi_person_studios(persons):
    animation_relations = {"动画制作", "動畫製作", "アニメーション制作"}
    studios = []
    for person in persons or []:
        if not isinstance(person, dict):
            continue
        relation = str(person.get("relation") or "").strip()
        name = str(person.get("name") or "").strip()
        if relation in animation_relations and name and name not in studios:
            studios.append(name)
    return " / ".join(studios)


def _bangumi_tags(item):
    tags = []
    for tag in item.get("tags") or []:
        values = _bangumi_text_values(tag)
        for value in values:
            if value not in tags:
                tags.append(value)
            if len(tags) == 8:
                return tags
    return tags


def _bangumi_headers():
    return {
        "User-Agent": getattr(settings, "BANGUMI_USER_AGENT", "AnimeJournal/1.0 (+https://xh-anime.com)"),
        "Accept": "application/json",
    }


def _bangumi_result(item, studio=None):
    images = item.get("images") or {}
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
    episodes = item.get("eps") or item.get("total_episodes") or ""
    source_poster = images.get("large") or images.get("common") or images.get("medium")
    if isinstance(episodes, list):
        episodes = len(episodes)
    return {
        "id": item.get("id"),
        "name": item.get("name_cn") or item.get("name") or "",
        "japanese_name": item.get("name") or "",
        "summary": item.get("summary") or "",
        "eps": episodes,
        "air_date": item.get("date") or item.get("air_date") or "",
        "studio": _bangumi_studio(item) if studio is None else studio,
        "tags": _bangumi_tags(item),
        "score": rating.get("score"),
        "poster": _bangumi_image_proxy(source_poster),
        "thumbnail": _bangumi_image_proxy(source_poster, resize=100),
        "url": f"https://bgm.tv/subject/{item.get('id')}" if item.get("id") else "https://bgm.tv/",
    }


def _bangumi_search_payload(query):
    headers = _bangumi_headers()
    failures = []

    try:
        response = requests.post(
            "https://api.bgm.tv/v0/search/subjects",
            params={"limit": 12, "offset": 0},
            json={"keyword": query, "sort": "match", "filter": {"type": [2]}},
            headers={**headers, "Content-Type": "application/json"},
            timeout=(4, 8),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Bangumi v0 returned an unexpected payload")
        return payload["data"]
    except (requests.RequestException, ValueError) as error:
        failures.append(f"v0:{type(error).__name__}:{error}")

    endpoint = f"https://api.bgm.tv/search/subject/{quote(query, safe='')}"
    try:
        response = requests.get(
            endpoint,
            params={"type": 2, "responseGroup": "large", "max_results": 12},
            headers=headers,
            timeout=(4, 8),
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("list", payload if isinstance(payload, list) else [])
        if not isinstance(items, list):
            raise ValueError("Bangumi legacy endpoint returned an unexpected payload")
        return items
    except (requests.RequestException, ValueError) as error:
        failures.append(f"legacy:{type(error).__name__}:{error}")

    logger.warning("Bangumi search failed for query=%r after both endpoints: %s", query, " | ".join(failures))
    raise requests.ConnectionError("Bangumi search endpoints are unavailable")


class BangumiSearchView(APIView):
    """Public proxy for Bangumi subject search with v0/legacy fallback."""

    permission_classes = [permissions.AllowAny]
    throttle_scope = "external_search"

    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if len(query) < 2:
            return Response({"results": []})

        cache_key = f"bangumi-search:v4:{query.casefold()}"
        cached_results = cache.get(cache_key)
        if cached_results is not None:
            return Response({"results": cached_results})

        try:
            items = _bangumi_search_payload(query)
        except requests.RequestException:
            return Response({"results": [], "detail": "Bangumi 暂时无法连接，请稍后重试。"}, status=status.HTTP_502_BAD_GATEWAY)

        results = [_bangumi_result(item) for item in items[:12]]
        cache.set(cache_key, results, timeout=300)
        return Response({"results": results})


class BangumiAutofillView(APIView):
    """Load full Bangumi metadata after a search result is selected."""

    permission_classes = [permissions.AllowAny]
    throttle_scope = "external_search"

    def get(self, request):
        try:
            subject_id = int(request.query_params.get("id", ""))
            if subject_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return Response({"detail": "请选择有效的 Bangumi 番剧。"}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"bangumi-autofill:v1:{subject_id}"
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return Response(cached_result)

        headers = _bangumi_headers()
        try:
            subject_response = requests.get(
                f"https://api.bgm.tv/v0/subjects/{subject_id}",
                headers=headers,
                timeout=(4, 8),
            )
            subject_response.raise_for_status()
            subject = subject_response.json()
            if not isinstance(subject, dict):
                raise ValueError("Bangumi subject returned an unexpected payload")
        except (requests.RequestException, ValueError) as error:
            logger.warning("Bangumi autofill subject failed for id=%s: %s", subject_id, error)
            return Response({"detail": "Bangumi 详情暂时无法读取，请稍后重试。"}, status=status.HTTP_502_BAD_GATEWAY)

        person_studio = ""
        try:
            persons_response = requests.get(
                f"https://api.bgm.tv/v0/subjects/{subject_id}/persons",
                headers=headers,
                timeout=(4, 8),
            )
            persons_response.raise_for_status()
            persons = persons_response.json()
            if not isinstance(persons, list):
                raise ValueError("Bangumi persons returned an unexpected payload")
            person_studio = _bangumi_person_studios(persons)
        except (requests.RequestException, ValueError) as error:
            logger.warning("Bangumi autofill persons failed for id=%s; using infobox fallback: %s", subject_id, error)

        result = _bangumi_result(subject, studio=person_studio or _bangumi_studio(subject))
        cache.set(cache_key, result, timeout=900)
        return Response(result)

