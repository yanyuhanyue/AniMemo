import json
import logging

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


class BangumiClientError(RuntimeError):
    def __init__(self, code, *, status_code=None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class BangumiClient:
    api_base_url = "https://api.bgm.tv"
    oauth_base_url = "https://bgm.tv/oauth"
    timeout = (4, 8)
    max_response_bytes = 2 * 1024 * 1024

    def headers(self, access_token=None):
        headers = {
            "User-Agent": getattr(
                settings,
                "BANGUMI_USER_AGENT",
                "AniMemo/1.0 (+https://animemo.cc)",
            ),
            "Accept": "application/json",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def request_json(
        self,
        method,
        path,
        *,
        endpoint,
        base="api",
        access_token=None,
        retry_get=True,
        not_found=False,
        **kwargs,
    ):
        method = str(method or "").lower()
        try:
            url = self._fixed_url(base, path)
        except (TypeError, ValueError) as error:
            raise BangumiClientError("invalid_response") from error
        attempts = 2 if retry_get and method == "get" else 1
        for attempt in range(attempts):
            response = None
            status_code = None
            try:
                response = requests.request(
                    method,
                    url,
                    headers={**self.headers(access_token), **kwargs.pop("headers", {})},
                    timeout=self.timeout,
                    stream=True,
                    **kwargs,
                )
                status_code = getattr(response, "status_code", None)
                if not_found and status_code == 404:
                    raise BangumiClientError("not_found", status_code=status_code)
                if status_code in {401, 403}:
                    raise BangumiClientError("unauthorized", status_code=status_code)
                if status_code in {502, 503} and attempt + 1 < attempts:
                    self._log_failure(endpoint, status_code, "RetryableStatus")
                    continue
                response.raise_for_status()
                raw = self._bounded_body(response)
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeError) as error:
                    raise BangumiClientError("invalid_response", status_code=status_code) from error
            except BangumiClientError:
                raise
            except requests.Timeout as error:
                if attempt + 1 < attempts:
                    self._log_failure(endpoint, status_code, type(error).__name__)
                    continue
                raise BangumiClientError("timeout", status_code=status_code) from error
            except requests.RequestException as error:
                status_code = getattr(getattr(error, "response", None), "status_code", status_code)
                if status_code in {502, 503} and attempt + 1 < attempts:
                    self._log_failure(endpoint, status_code, type(error).__name__)
                    continue
                raise BangumiClientError("unavailable", status_code=status_code) from error
            except (TypeError, ValueError) as error:
                raise BangumiClientError("invalid_response", status_code=status_code) from error
            finally:
                if response is not None:
                    response.close()
        raise BangumiClientError("unavailable")

    def _bounded_body(self, response):
        content_length = str(getattr(response, "headers", {}).get("Content-Length") or "").strip()
        if content_length:
            try:
                if int(content_length) > self.max_response_bytes:
                    raise BangumiClientError("invalid_response", status_code=response.status_code)
            except ValueError as error:
                raise BangumiClientError("invalid_response", status_code=response.status_code) from error
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > self.max_response_bytes:
                raise BangumiClientError("invalid_response", status_code=response.status_code)
            chunks.append(chunk)
        return b"".join(chunks)

    def _fixed_url(self, base, path):
        normalized = str(path or "")
        if not normalized.startswith("/") or "://" in normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("Bangumi path must be a fixed relative endpoint")
        root = self.api_base_url if base == "api" else self.oauth_base_url if base == "oauth" else ""
        if not root:
            raise ValueError("Unknown Bangumi endpoint base")
        return f"{root.rstrip('/')}/{normalized.lstrip('/')}"

    @staticmethod
    def _log_failure(endpoint, status_code, error_class):
        logger.warning(
            "Bangumi request failed endpoint=%s status=%s error=%s",
            endpoint,
            status_code if status_code is not None else "unknown",
            error_class,
        )
