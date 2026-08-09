import json
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from journal.bangumi import BangumiClient, BangumiClientError
from journal.external_media.errors import ExternalMediaError
from journal.external_media.providers.bangumi import BangumiProvider


def http_response(payload=None, *, status_code=200, raw=None, headers=None):
    result = Mock()
    result.status_code = status_code
    result.headers = headers or {}
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    result.iter_content.return_value = [body]
    if status_code >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    else:
        result.raise_for_status.return_value = None
    return result


class BangumiProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = Mock(spec=BangumiClient)
        self.provider = BangumiProvider(client=self.client)

    def test_positive_integer_external_id_is_normalized_as_string(self):
        self.assertEqual(self.provider.normalize_external_id("001424"), "1424")
        self.assertEqual(self.provider.canonical_url("1424"), "https://bgm.tv/subject/1424")

    def test_invalid_external_ids_are_rejected_without_http(self):
        for value in (None, "", "0", "-1", "1.5", "abc", "１２３", "1" * 21):
            with self.subTest(value=value), self.assertRaises(ExternalMediaError) as caught:
                self.provider.normalize_external_id(value)
            self.assertEqual(caught.exception.detail["code"], "invalid_external_id")
        self.client.request_json.assert_not_called()

    def test_normalization_prefers_chinese_title_and_returns_only_unified_fields(self):
        normalized = self.provider.normalize_subject({
            "id": 1424,
            "name": "けいおん！",
            "name_cn": "轻音少女",
            "summary": "轻音部的故事。",
            "date": "2009-04-03",
            "total_episodes": 14,
            "rating": {"score": 8.2},
            "tags": [{"name": "校园"}, {"name": "日常"}],
            "images": {"large": "http://lain.bgm.tv/pic/cover/l/k-on.jpg"},
        })
        self.assertEqual(normalized["title"], "轻音少女")
        self.assertEqual(normalized["japanese_title"], "けいおん！")
        self.assertEqual(normalized["episodes"], 14)
        self.assertEqual(normalized["score"], 8.2)
        self.assertEqual(normalized["canonical_url"], "https://bgm.tv/subject/1424")
        self.assertEqual(set(normalized), {
            "provider", "external_id", "title", "japanese_title", "summary",
            "episodes", "air_date", "studio", "tags", "score", "poster_url",
            "thumbnail_url", "canonical_url",
        })

    def test_unknown_or_invalid_episode_count_becomes_none(self):
        self.assertIsNone(self.provider.normalize_subject({"id": 1, "name": "A", "eps": 0})["episodes"])
        self.assertIsNone(self.provider.normalize_subject({"id": 2, "name": "B", "eps": None})["episodes"])
        self.assertIsNone(self.provider.normalize_subject({"id": 3, "name": "C", "eps": 1000000})["episodes"])

    def test_normalized_snapshot_bounds_provider_text_and_urls(self):
        normalized = self.provider.normalize_subject({
            "id": 3,
            "name": "日" * 700,
            "name_cn": "中" * 700,
            "summary": "摘" * 6000,
            "images": {"large": f"https://lain.bgm.tv/{'x' * 1100}"},
            "rating": {"score": 99},
        })
        self.assertEqual(len(normalized["title"]), 500)
        self.assertEqual(len(normalized["japanese_title"]), 500)
        self.assertEqual(len(normalized["summary"]), 5000)
        self.assertEqual(normalized["poster_url"], "")
        self.assertIsNone(normalized["score"])

    def test_studio_prefers_person_animation_relations(self):
        normalized = self.provider.normalize_subject(
            {"id": 3, "name": "A", "infobox": [{"key": "製作", "value": "Committee"}]},
            persons=[
                {"name": "Studio A", "relation": "动画制作"},
                {"name": "Studio B", "relation": "アニメーション制作"},
                {"name": "Committee", "relation": "製作"},
            ],
        )
        self.assertEqual(normalized["studio"], "Studio A / Studio B")

    def test_studio_falls_back_to_prioritized_infobox_keys(self):
        normalized = self.provider.normalize_subject({
            "id": 4,
            "name": "A",
            "infobox": [
                {"key": "製作", "value": "Committee"},
                {"key": "动画制作", "value": [{"v": "Studio A"}]},
            ],
        })
        self.assertEqual(normalized["studio"], "Studio A")

    def test_tags_are_deduplicated_and_limited(self):
        normalized = self.provider.normalize_subject({
            "id": 5,
            "name": "A",
            "tags": [{"name": value} for value in ["A", "A", "B", "C", "D", "E", "F", "G", "H", "I"]],
        })
        self.assertEqual(normalized["tags"], ["A", "B", "C", "D", "E", "F", "G", "H"])

    @override_settings(BANGUMI_IMAGE_PROXY_BASE_URL="https://proxy.example.test/base/")
    def test_image_proxy_is_configurable(self):
        normalized = self.provider.normalize_subject({
            "id": 6,
            "name": "A",
            "images": {"large": "http://lain.bgm.tv/pic/cover/l/a.jpg"},
        })
        self.assertEqual(normalized["poster_url"], "https://proxy.example.test/base/pic/cover/l/a.jpg")
        self.assertEqual(normalized["thumbnail_url"], "https://proxy.example.test/base/r/100/pic/cover/l/a.jpg")

    @override_settings(BANGUMI_IMAGE_PROXY_BASE_URL="")
    def test_empty_image_proxy_uses_https_bangumi_image(self):
        normalized = self.provider.normalize_subject({
            "id": 7,
            "name": "A",
            "images": {"large": "http://lain.bgm.tv/pic/cover/l/a.jpg"},
        })
        self.assertEqual(normalized["poster_url"], "https://lain.bgm.tv/pic/cover/l/a.jpg")

    def test_non_bangumi_image_host_is_not_exposed(self):
        normalized = self.provider.normalize_subject({
            "id": 8,
            "name": "A",
            "images": {"large": "https://attacker.example/cover.jpg"},
        })
        self.assertEqual(normalized["poster_url"], "")

    def test_search_uses_only_official_v0_endpoint_and_unified_dto(self):
        self.client.request_json.return_value = {"data": [{"id": 9, "name": "原名", "name_cn": "中文名"}]}
        result = self.provider.search("中文名-v0")
        self.assertEqual(result[0]["external_id"], "9")
        self.assertEqual(result[0]["title"], "中文名")
        self.assertNotIn("id", result[0])
        self.client.request_json.assert_called_once_with(
            "post",
            "/v0/search/subjects",
            endpoint="search",
            retry_get=False,
            params={"limit": 12, "offset": 0},
            json={"keyword": "中文名-v0", "sort": "match", "filter": {"type": [2]}},
            headers={"Content-Type": "application/json"},
        )

    def test_search_uses_cache_and_force_bypasses_it(self):
        self.client.request_json.side_effect = [
            {"data": [{"id": 10, "name": "First"}]},
            {"data": [{"id": 10, "name": "Second"}]},
        ]
        self.assertEqual(self.provider.search("cache query")[0]["title"], "First")
        self.assertEqual(self.provider.search("cache query")[0]["title"], "First")
        self.assertEqual(self.provider.search("cache query", force=True)[0]["title"], "Second")
        self.assertEqual(self.client.request_json.call_count, 2)

    def test_search_timeout_is_mapped_without_upstream_content(self):
        self.client.request_json.side_effect = BangumiClientError("timeout")
        with self.assertRaises(ExternalMediaError) as caught:
            self.provider.search("timeout-case")
        self.assertEqual(caught.exception.detail["code"], "provider_timeout")

    def test_search_invalid_responses_are_mapped(self):
        self.client.request_json.return_value = {"unexpected": []}
        with self.assertRaises(ExternalMediaError) as caught:
            self.provider.search("invalid-response")
        self.assertEqual(caught.exception.detail["code"], "provider_invalid_response")

    def test_subject_fetch_uses_cache_and_force_bypasses_it(self):
        self.client.request_json.side_effect = [
            {"id": 11, "name": "First"}, [],
            {"id": 11, "name": "Second"}, [],
        ]
        first = self.provider.fetch_subject("11")
        cached = self.provider.fetch_subject("11")
        refreshed = self.provider.fetch_subject("11", force=True)
        self.assertEqual(first["title"], "First")
        self.assertEqual(cached["title"], "First")
        self.assertEqual(refreshed["title"], "Second")
        self.assertEqual(self.client.request_json.call_count, 4)

    def test_subject_not_found_is_mapped(self):
        self.client.request_json.side_effect = BangumiClientError("not_found", status_code=404)
        with self.assertRaises(ExternalMediaError) as caught:
            self.provider.fetch_subject("404")
        self.assertEqual(caught.exception.detail["code"], "subject_not_found")

    def test_invalid_subject_is_mapped_to_invalid_response(self):
        self.client.request_json.return_value = {"id": 999, "name": "Wrong"}
        with self.assertRaises(ExternalMediaError) as caught:
            self.provider.fetch_subject("405", force=True)
        self.assertEqual(caught.exception.detail["code"], "provider_invalid_response")

    def test_persons_failure_keeps_infobox_studio(self):
        self.client.request_json.side_effect = [
            {"id": 12, "name": "A", "infobox": [{"key": "动画制作", "value": "Studio"}]},
            BangumiClientError("timeout"),
        ]
        self.assertEqual(self.provider.fetch_subject("12")["studio"], "Studio")


class BangumiClientTests(SimpleTestCase):
    @override_settings(BANGUMI_USER_AGENT="AniMemo-Test/2.0 (+https://example.test)")
    def test_configured_user_agent_and_authorization_are_used(self):
        headers = BangumiClient().headers("token-value")
        self.assertEqual(headers["User-Agent"], "AniMemo-Test/2.0 (+https://example.test)")
        self.assertEqual(headers["Authorization"], "Bearer token-value")

    @patch("journal.bangumi.client.requests.request")
    def test_get_uses_fixed_endpoint_and_retries_retryable_status(self, request):
        first = http_response({}, status_code=503)
        second = http_response({"ok": True})
        request.side_effect = [first, second]
        result = BangumiClient().request_json("get", "/v0/me", endpoint="me")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[1], "https://api.bgm.tv/v0/me")
        first.close.assert_called_once()
        second.close.assert_called_once()

    @patch("journal.bangumi.client.requests.request")
    def test_oauth_post_is_not_retried(self, request):
        request.side_effect = requests.Timeout("secret token text")
        with self.assertRaises(BangumiClientError) as caught:
            BangumiClient().request_json(
                "post",
                "/access_token",
                base="oauth",
                endpoint="oauth_exchange",
                retry_get=False,
            )
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(request.call_count, 1)

    @patch("journal.bangumi.client.requests.request")
    def test_response_body_is_bounded_and_closed(self, request):
        oversized = http_response(
            raw=b"{}",
            headers={"Content-Length": str(BangumiClient.max_response_bytes + 1)},
        )
        request.return_value = oversized
        with self.assertRaises(BangumiClientError) as caught:
            BangumiClient().request_json("get", "/v0/me", endpoint="me")
        self.assertEqual(caught.exception.code, "invalid_response")
        oversized.close.assert_called_once()

    def test_arbitrary_urls_and_unknown_bases_are_rejected_without_http(self):
        client = BangumiClient()
        for path, base in (("https://attacker.example/", "api"), ("/v0/me", "unknown")):
            with self.subTest(path=path, base=base), self.assertRaises(BangumiClientError) as caught:
                client.request_json("get", path, base=base, endpoint="test")
            self.assertEqual(caught.exception.code, "invalid_response")
