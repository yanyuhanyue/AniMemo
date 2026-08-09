from urllib.parse import quote, urlencode

from django.conf import settings

from journal.bangumi import (
    BangumiClient,
    BangumiClientError,
    image_url,
    normalize_external_id,
)
from journal.external_sync.canonical import MISSING, canonical_snapshot

from ..errors import (
    ExternalAccountError,
    account_token_invalid,
    authorization_exchange_failed,
    provider_invalid_response,
    provider_unavailable,
)


class BangumiAccountProvider:
    slug = "bangumi"
    display_name = "Bangumi"
    oauth_base_url = "https://bgm.tv/oauth"
    collection_page_size = 50
    collection_statuses = {
        1: ("planned", "想看"),
        2: ("completed", "看过"),
        3: ("watching", "在看"),
        4: ("on_hold", "搁置"),
        5: ("dropped", "抛弃"),
    }

    def __init__(self, client=None):
        self.client = client or BangumiClient()

    def headers(self, access_token=None):
        return self.client.headers(access_token)

    def enabled(self):
        return bool(getattr(settings, "BANGUMI_ACCOUNT_INTEGRATION_ENABLED", True))

    def capabilities(self):
        enabled = self.enabled()
        return {
            "provider": self.slug,
            "display_name": self.display_name,
            "media_search_available": True,
            "account_connection_available": enabled,
            "oauth_available": enabled and self.oauth_available(),
            "personal_access_token_available": enabled,
            "import_available": enabled,
            "collection_sync_preview_available": enabled,
            "collection_sync_pull_available": enabled,
            "collection_sync_apply_available": enabled,
            "collection_sync_push_available": False,
            "collection_write_implemented": False,
        }

    def import_max_items(self):
        return int(settings.BANGUMI_IMPORT_MAX_ITEMS)

    def oauth_available(self):
        return all(
            str(getattr(settings, name, "") or "").strip()
            for name in ("BANGUMI_OAUTH_CLIENT_ID", "BANGUMI_OAUTH_CLIENT_SECRET", "BANGUMI_OAUTH_REDIRECT_URI")
        )

    def authorization_url(self, state):
        params = {
            "client_id": settings.BANGUMI_OAUTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.BANGUMI_OAUTH_REDIRECT_URI,
            "state": state,
        }
        return f"{self.oauth_base_url}/authorize?{urlencode(params)}"

    def exchange_code(self, code, state):
        payload = self._request_json(
            "post",
            "/access_token",
            endpoint="oauth_exchange",
            base="oauth",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.BANGUMI_OAUTH_CLIENT_ID,
                "client_secret": settings.BANGUMI_OAUTH_CLIENT_SECRET,
                "code": str(code or "")[:512],
                "redirect_uri": settings.BANGUMI_OAUTH_REDIRECT_URI,
                "state": str(state or "")[:512],
            },
        )
        return self._normalize_token_payload(payload)

    def refresh_oauth_token(self, refresh_token):
        payload = self._request_json(
            "post",
            "/access_token",
            endpoint="oauth_refresh",
            base="oauth",
            data={
                "grant_type": "refresh_token",
                "client_id": settings.BANGUMI_OAUTH_CLIENT_ID,
                "client_secret": settings.BANGUMI_OAUTH_CLIENT_SECRET,
                "refresh_token": str(refresh_token or "")[:4096],
                "redirect_uri": settings.BANGUMI_OAUTH_REDIRECT_URI,
            },
        )
        return self._normalize_token_payload(payload)

    def verify_account(self, access_token):
        payload = self._request_json(
            "get",
            "/v0/me",
            endpoint="me",
            access_token=self._validate_token(access_token),
        )
        if not isinstance(payload, dict):
            raise provider_invalid_response()
        external_user_id = str(payload.get("id") or "").strip()
        username = str(payload.get("username") or "").strip()
        if not external_user_id.isascii() or not external_user_id.isdigit() or not username:
            raise provider_invalid_response()
        avatars = payload.get("avatar") if isinstance(payload.get("avatar"), dict) else {}
        avatar_url = image_url(avatars.get("large") or avatars.get("medium") or avatars.get("small"))
        return {
            "external_user_id": external_user_id[:200],
            "external_username": username[:200],
            "display_name": str(payload.get("nickname") or username).strip()[:200],
            "metadata": {"avatar_url": avatar_url},
        }

    def get_collections(self, access_token, username, *, max_items):
        token = self._validate_token(access_token)
        username = str(username or "").strip()
        if not username or len(username) > 200:
            raise provider_invalid_response()
        max_items = max(1, int(max_items))
        rows = []
        seen_external_ids = set()
        offset = 0
        total = None
        page_count = 0
        max_pages = max(1, (max_items + self.collection_page_size - 1) // self.collection_page_size + 1)
        while len(rows) < max_items and page_count < max_pages:
            page_count += 1
            payload = self._request_json(
                "get",
                f"/v0/users/{quote(username, safe='')}/collections",
                endpoint="collections",
                access_token=token,
                params={"subject_type": 2, "limit": self.collection_page_size, "offset": offset},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise provider_invalid_response()
            page = payload["data"]
            try:
                total = max(0, int(payload.get("total", len(page))))
            except (TypeError, ValueError):
                raise provider_invalid_response()
            for item in page:
                normalized = self.normalize_collection(item)
                if normalized is not None and normalized["external_id"] not in seen_external_ids:
                    seen_external_ids.add(normalized["external_id"])
                    rows.append(normalized)
                    if len(rows) == max_items:
                        break
            if not page:
                break
            next_offset = offset + len(page)
            if next_offset <= offset or next_offset >= total:
                break
            offset = next_offset
        return rows

    def get_collection(self, access_token, username, external_id):
        external_id = self._normalize_external_id(external_id)
        username = str(username or "").strip()
        if not username or len(username) > 200:
            raise provider_invalid_response()
        payload = self._request_json(
            "get",
            f"/v0/users/{quote(username, safe='')}/collections/{external_id}",
            endpoint="collection",
            access_token=self._validate_token(access_token),
            not_found=True,
            allow_not_found=True,
        )
        if payload is None:
            return None
        normalized = self.normalize_collection(payload)
        if normalized is None:
            raise provider_invalid_response()
        return normalized

    def normalize_collection(self, item):
        if not isinstance(item, dict) or item.get("subject_type") != 2:
            return None
        subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
        external_id = self._normalize_external_id(item.get("subject_id") or subject.get("id"))
        status_code = item.get("type")
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            raise provider_invalid_response()
        status_info = self.collection_statuses.get(status_code)
        if status_info is None:
            raise provider_invalid_response()
        rating = self._rating(item.get("rate"))
        tags = []
        for value in item.get("tags") or []:
            value = str(value or "").strip()[:100]
            if value and value not in tags:
                tags.append(value)
            if len(tags) == 30:
                break
        raw_comment = item.get("comment")
        if raw_comment is not None and not isinstance(raw_comment, str):
            raise provider_invalid_response()
        comment_present = raw_comment is not None
        comment = raw_comment or ""
        if len(comment) > 10000:
            raise provider_invalid_response()
        images = subject.get("images") if isinstance(subject.get("images"), dict) else {}
        poster_url = image_url(images.get("large") or images.get("common") or images.get("medium"))
        title = str(subject.get("name_cn") or subject.get("name") or f"Bangumi #{external_id}").strip()[:500]
        japanese_title = str(subject.get("name") or "").strip()[:500]
        return {
            "provider": self.slug,
            "external_id": external_id,
            "title": title,
            "japanese_title": japanese_title,
            "poster_url": poster_url,
            "remote_status": status_info[0],
            "remote_status_label": status_info[1],
            "remote_status_code": status_code,
            "remote_rating": rating,
            "remote_rating_present": rating is not None,
            "remote_comment": comment,
            "remote_comment_present": comment_present,
            "remote_comment_summary": comment[:160],
            "remote_tags": tags,
            "remote_updated_at": str(item.get("updated_at") or "")[:64],
        }

    def collection_sync_snapshot(self, collection):
        if collection is None:
            return None
        return canonical_snapshot(
            watch_status=collection["remote_status"],
            personal_score=(
                collection["remote_rating"]
                if collection.get("remote_rating_present")
                else MISSING
            ),
            review=(
                collection["remote_comment"]
                if collection.get("remote_comment_present")
                else MISSING
            ),
        )

    @staticmethod
    def collection_push_capabilities(local):
        score = local["personal_score"]
        if not score["present"]:
            score_capability = {"supported": True, "reason": None}
        elif local["watch_status"]["value"] == "planned":
            score_capability = {"supported": False, "reason": "planned_status_forces_score_clear"}
        elif score["value"] == "0":
            score_capability = {"supported": False, "reason": "zero_score_represents_clear"}
        elif "." in score["value"]:
            score_capability = {"supported": False, "reason": "fractional_score_not_supported"}
        else:
            score_capability = {"supported": True, "reason": None}
        review = local["review"]
        if review["present"] and len(review["value"]) > 380:
            review_capability = {"supported": False, "reason": "review_exceeds_provider_limit"}
        elif review["present"] and not BangumiAccountProvider._comment_is_printable(review["value"]):
            review_capability = {"supported": False, "reason": "review_contains_unsupported_characters"}
        else:
            review_capability = {"supported": True, "reason": None}
        return {
            "watch_status": {"supported": True, "reason": None},
            "personal_score": score_capability,
            "review": review_capability,
        }

    @staticmethod
    def _comment_is_printable(value):
        return all(character in "\n\t\r" or character.isprintable() for character in value)

    @staticmethod
    def _rating(value):
        try:
            rating = int(value)
        except (TypeError, ValueError):
            return None
        return rating if 1 <= rating <= 10 else None

    @staticmethod
    def _validate_token(value):
        token = str(value or "").strip()
        if not 8 <= len(token) <= 4096 or any(character.isspace() for character in token):
            raise account_token_invalid()
        return token

    def _normalize_token_payload(self, payload):
        if not isinstance(payload, dict):
            raise authorization_exchange_failed()
        try:
            access_token = self._validate_token(payload.get("access_token"))
        except ExternalAccountError as error:
            raise authorization_exchange_failed() from error
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if refresh_token and (len(refresh_token) > 4096 or any(character.isspace() for character in refresh_token)):
            raise authorization_exchange_failed()
        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            raise authorization_exchange_failed()
        if not 1 <= expires_in <= 366 * 24 * 60 * 60:
            raise authorization_exchange_failed()
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }

    def _request_json(
        self,
        method,
        path,
        *,
        endpoint,
        access_token=None,
        base="api",
        allow_not_found=False,
        **kwargs,
    ):
        try:
            return self.client.request_json(
                method,
                path,
                endpoint=endpoint,
                base=base,
                access_token=access_token,
                retry_get=True,
                **kwargs,
            )
        except BangumiClientError as error:
            if error.code == "not_found" and allow_not_found:
                return None
            if endpoint in {"oauth_exchange", "oauth_refresh"}:
                raise authorization_exchange_failed() from error
            if error.code == "unauthorized":
                raise account_token_invalid() from error
            if error.code == "invalid_response":
                raise provider_invalid_response() from error
            raise provider_unavailable() from error

    @staticmethod
    def _normalize_external_id(value):
        try:
            return normalize_external_id(value)
        except ValueError as error:
            raise provider_invalid_response() from error
