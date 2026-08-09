import logging
from urllib.parse import quote, urlparse

import requests
from django.conf import settings
from django.core.cache import cache

from ..errors import (
    ExternalMediaError,
    invalid_external_id,
    provider_invalid_response,
    provider_timeout,
    provider_unavailable,
    subject_not_found,
)

logger = logging.getLogger(__name__)


class BangumiProvider:
    slug = "bangumi"
    display_name = "Bangumi"
    api_base_url = "https://api.bgm.tv"
    canonical_base_url = "https://bgm.tv/subject/"
    timeout = (4, 8)
    search_cache_timeout = 300
    subject_cache_timeout = 900

    def headers(self):
        return {
            "User-Agent": getattr(settings, "BANGUMI_USER_AGENT", "AniMemo/1.0 (+https://re-anime.cc)"),
            "Accept": "application/json",
        }

    def normalize_external_id(self, value):
        normalized = str(value or "").strip()
        if (
            not normalized.isascii()
            or not normalized.isdigit()
            or len(normalized) > 20
            or int(normalized) <= 0
        ):
            raise invalid_external_id("请选择有效的 Bangumi 番剧。")
        return str(int(normalized))

    def canonical_url(self, external_id):
        return f"{self.canonical_base_url}{self.normalize_external_id(external_id)}"

    def search(self, query):
        normalized_query = str(query or "").strip()[:100]
        if len(normalized_query) < 2:
            return []
        cache_key = f"bangumi-search:v5:{normalized_query.casefold()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        failures = []
        try:
            payload = self._request_json(
                "post",
                f"{self.api_base_url}/v0/search/subjects",
                endpoint="search_v0",
                params={"limit": 12, "offset": 0},
                json={"keyword": normalized_query, "sort": "match", "filter": {"type": [2]}},
                headers={**self.headers(), "Content-Type": "application/json"},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise provider_invalid_response()
            items = payload["data"]
        except ExternalMediaError as error:
            failures.append(error)
            try:
                payload = self._request_json(
                    "get",
                    f"{self.api_base_url}/search/subject/{quote(normalized_query, safe='')}",
                    endpoint="search_legacy",
                    params={"type": 2, "responseGroup": "large", "max_results": 12},
                    headers=self.headers(),
                )
                items = payload.get("list", payload if isinstance(payload, list) else None)
                if not isinstance(items, list):
                    raise provider_invalid_response()
            except ExternalMediaError as legacy_error:
                failures.append(legacy_error)
                logger.warning(
                    "External media request failed provider=bangumi endpoint=search failures=%s query_length=%s",
                    ",".join(item.detail["code"] for item in failures),
                    len(normalized_query),
                )
                if any(item.detail["code"] == "provider_timeout" for item in failures):
                    raise provider_timeout()
                if all(item.detail["code"] == "provider_invalid_response" for item in failures):
                    raise provider_invalid_response()
                raise provider_unavailable()

        results = [self.legacy_result(self.normalize_subject(item)) for item in items[:12] if isinstance(item, dict)]
        cache.set(cache_key, results, timeout=self.search_cache_timeout)
        return results

    def fetch_subject(self, external_id, *, force=False):
        normalized_id = self.normalize_external_id(external_id)
        cache_key = f"bangumi-subject:v2:{normalized_id}"
        if not force:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        subject = self._request_json(
            "get",
            f"{self.api_base_url}/v0/subjects/{normalized_id}",
            endpoint="subject",
            headers=self.headers(),
            not_found=True,
        )
        if not isinstance(subject, dict) or str(subject.get("id") or "") != normalized_id:
            raise provider_invalid_response()

        persons = []
        try:
            persons_payload = self._request_json(
                "get",
                f"{self.api_base_url}/v0/subjects/{normalized_id}/persons",
                endpoint="persons",
                headers=self.headers(),
            )
            if not isinstance(persons_payload, list):
                raise provider_invalid_response()
            persons = persons_payload
        except ExternalMediaError as error:
            logger.warning(
                "External media request failed provider=bangumi endpoint=persons subject_id=%s code=%s; using infobox",
                normalized_id,
                error.detail["code"],
            )

        normalized = self.normalize_subject(subject, persons=persons)
        cache.set(cache_key, normalized, timeout=self.subject_cache_timeout)
        return normalized

    def refresh(self, identity):
        return self.fetch_subject(identity.external_id, force=True)

    def normalize_subject(self, item, *, persons=None):
        external_id = self.normalize_external_id(item.get("id"))
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
        person_studio = self._person_studios(persons or [])
        score = self._score(rating.get("score"))
        return {
            "title": self._bounded_text(item.get("name_cn") or item.get("name"), 500),
            "japanese_title": self._bounded_text(item.get("name"), 500),
            "summary": self._bounded_text(item.get("summary"), 5000),
            "episodes": episodes,
            "air_date": self._bounded_text(item.get("date") or item.get("air_date"), 32),
            "studio": self._bounded_text(person_studio or self._studio(item), 500),
            "tags": self._tags(item),
            "score": score,
            "poster_url": self._image_url(source_poster),
            "thumbnail_url": self._image_url(source_poster, resize=100),
            "provider_name": self.display_name,
            "provider_url": self.canonical_url(external_id),
            "external_id": external_id,
        }

    def legacy_result(self, metadata):
        external_id = metadata["external_id"]
        return {
            "id": int(external_id),
            "name": metadata["title"],
            "japanese_name": metadata["japanese_title"],
            "summary": metadata["summary"],
            "eps": metadata["episodes"] or "",
            "air_date": metadata["air_date"],
            "studio": metadata["studio"],
            "tags": metadata["tags"],
            "score": metadata["score"],
            "poster": metadata["poster_url"],
            "thumbnail": metadata["thumbnail_url"],
            "url": metadata["provider_url"],
            "provider": self.slug,
            "external_id": external_id,
        }

    def _request_json(self, method, url, *, endpoint, not_found=False, **kwargs):
        response_status = None
        try:
            response = getattr(requests, method)(url, timeout=self.timeout, **kwargs)
            response_status = getattr(response, "status_code", None)
            if not_found and response_status == 404:
                self._log_failure(endpoint, response_status, "NotFound")
                raise subject_not_found()
            response.raise_for_status()
            return response.json()
        except ExternalMediaError:
            raise
        except requests.Timeout as error:
            self._log_failure(endpoint, response_status, type(error).__name__)
            raise provider_timeout() from error
        except requests.exceptions.JSONDecodeError as error:
            self._log_failure(endpoint, response_status, type(error).__name__)
            raise provider_invalid_response() from error
        except requests.RequestException as error:
            status_code = getattr(getattr(error, "response", None), "status_code", response_status)
            self._log_failure(endpoint, status_code, type(error).__name__)
            raise provider_unavailable() from error
        except (TypeError, ValueError) as error:
            self._log_failure(endpoint, response_status, type(error).__name__)
            raise provider_invalid_response() from error

    def _log_failure(self, endpoint, status_code, error_class):
        logger.warning(
            "External media request failed provider=%s endpoint=%s status=%s error=%s",
            self.slug,
            endpoint,
            status_code if status_code is not None else "unknown",
            error_class,
        )

    def _image_url(self, value, *, resize=None):
        url = self._https_url(value)
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

    @staticmethod
    def _https_url(value):
        url = str(value or "").strip()
        return f"https://{url[7:]}" if url.startswith("http://") else url

    @classmethod
    def _text_values(cls, value):
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (int, float)):
            return [str(value)]
        if isinstance(value, list):
            values = []
            for item in value:
                values.extend(cls._text_values(item))
            return values
        if isinstance(value, dict):
            for key in ("v", "value", "name", "title"):
                if key in value:
                    return cls._text_values(value[key])
        return []

    @staticmethod
    def _bounded_text(value, max_length):
        return str(value or "").strip()[:max_length]

    @staticmethod
    def _score(value):
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        return score if 0 <= score <= 10 else None

    @classmethod
    def _studio(cls, item):
        infobox = [info for info in item.get("infobox") or [] if isinstance(info, dict)]
        for accepted_keys in (
            {"动画制作", "動畫製作", "アニメーション制作"},
            {"制作公司", "製作公司"},
            {"制作", "製作"},
        ):
            for info in infobox:
                if str(info.get("key") or "").strip() not in accepted_keys:
                    continue
                values = cls._text_values(info.get("value"))
                if values:
                    return " / ".join(dict.fromkeys(values))
        return ""

    @staticmethod
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

    @classmethod
    def _tags(cls, item):
        tags = []
        for tag in item.get("tags") or []:
            for value in cls._text_values(tag):
                value = cls._bounded_text(value, 100)
                if value not in tags:
                    tags.append(value)
                if len(tags) == 8:
                    return tags
        return tags
