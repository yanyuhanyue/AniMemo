import logging
from hashlib import sha256

from django.core.cache import cache

from journal.bangumi import (
    BangumiClient,
    BangumiClientError,
    canonical_url,
    normalize_external_id,
    normalize_subject,
)

from ..errors import (
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
    search_cache_timeout = 300
    subject_cache_timeout = 900

    def __init__(self, client=None):
        self.client = client or BangumiClient()

    def headers(self):
        return self.client.headers()

    def normalize_external_id(self, value):
        try:
            return normalize_external_id(value)
        except ValueError as error:
            raise invalid_external_id("请选择有效的 Bangumi 番剧。") from error

    def canonical_url(self, external_id):
        try:
            return canonical_url(external_id)
        except ValueError as error:
            raise invalid_external_id("请选择有效的 Bangumi 番剧。") from error

    def search(self, query, *, force=False):
        normalized_query = str(query or "").strip()[:100]
        if len(normalized_query) < 2:
            return []
        query_digest = sha256(normalized_query.casefold().encode("utf-8")).hexdigest()
        cache_key = f"external-media:bangumi:search:v1:{query_digest}"
        if not force:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            payload = self.client.request_json(
                "post",
                "/v0/search/subjects",
                endpoint="search",
                retry_get=False,
                params={"limit": 12, "offset": 0},
                json={"keyword": normalized_query, "sort": "match", "filter": {"type": [2]}},
                headers={"Content-Type": "application/json"},
            )
        except BangumiClientError as error:
            raise self._domain_error(error) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise provider_invalid_response()
        try:
            results = [normalize_subject(item) for item in payload["data"][:12] if isinstance(item, dict)]
        except (TypeError, ValueError) as error:
            raise provider_invalid_response() from error
        cache.set(cache_key, results, timeout=self.search_cache_timeout)
        return results

    def fetch_subject(self, external_id, *, force=False):
        normalized_id = self.normalize_external_id(external_id)
        cache_key = f"external-media:bangumi:subject:v1:{normalized_id}"
        if not force:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            subject = self.client.request_json(
                "get",
                f"/v0/subjects/{normalized_id}",
                endpoint="subject",
                not_found=True,
            )
        except BangumiClientError as error:
            raise self._domain_error(error) from error
        if not isinstance(subject, dict) or str(subject.get("id") or "") != normalized_id:
            raise provider_invalid_response()

        persons = []
        try:
            persons_payload = self.client.request_json(
                "get",
                f"/v0/subjects/{normalized_id}/persons",
                endpoint="persons",
            )
            if not isinstance(persons_payload, list):
                raise provider_invalid_response()
            persons = persons_payload
        except (BangumiClientError, TypeError, ValueError) as error:
            code = error.code if isinstance(error, BangumiClientError) else "invalid_response"
            logger.warning(
                "External media request failed provider=bangumi endpoint=persons subject_id=%s code=%s; using infobox",
                normalized_id,
                code,
            )
        try:
            normalized = normalize_subject(subject, persons=persons)
        except (TypeError, ValueError) as error:
            raise provider_invalid_response() from error
        cache.set(cache_key, normalized, timeout=self.subject_cache_timeout)
        return normalized

    def refresh(self, identity):
        return self.fetch_subject(identity.external_id, force=True)

    def normalize_subject(self, item, *, persons=None):
        try:
            return normalize_subject(item, persons=persons)
        except ValueError as error:
            raise invalid_external_id("请选择有效的 Bangumi 番剧。") from error

    @staticmethod
    def _domain_error(error):
        if error.code == "timeout":
            return provider_timeout()
        if error.code == "not_found":
            return subject_not_found()
        if error.code == "invalid_response":
            return provider_invalid_response()
        return provider_unavailable()
