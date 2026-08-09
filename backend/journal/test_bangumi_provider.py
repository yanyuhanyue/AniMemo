from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from journal.external_media.errors import ExternalMediaError
from journal.external_media.providers.bangumi import BangumiProvider


def response(payload, *, status_code=200):
    result = Mock()
    result.status_code = status_code
    result.raise_for_status.return_value = None
    result.json.return_value = payload
    return result


class BangumiProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.provider = BangumiProvider()

    def test_positive_integer_external_id_is_normalized_as_string(self):
        self.assertEqual(self.provider.normalize_external_id("001424"), "1424")
        self.assertEqual(self.provider.canonical_url("1424"), "https://bgm.tv/subject/1424")

    def test_invalid_external_ids_are_rejected_without_http(self):
        for value in (None, "", "0", "-1", "1.5", "abc", "１２３", "1" * 21):
            with self.subTest(value=value), self.assertRaises(ExternalMediaError) as caught:
                self.provider.normalize_external_id(value)
            self.assertEqual(caught.exception.detail["code"], "invalid_external_id")

    @override_settings(BANGUMI_USER_AGENT="AniMemo-Test/2.0 (+https://example.test)")
    def test_configured_user_agent_is_used(self):
        self.assertEqual(self.provider.headers()["User-Agent"], "AniMemo-Test/2.0 (+https://example.test)")

    def test_normalization_prefers_chinese_title_and_preserves_metadata(self):
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
        self.assertEqual(normalized["provider_url"], "https://bgm.tv/subject/1424")
        self.assertNotIn("rating", normalized)

    def test_unknown_or_zero_episode_count_becomes_none(self):
        self.assertIsNone(self.provider.normalize_subject({"id": 1, "name": "A", "eps": 0})["episodes"])
        self.assertIsNone(self.provider.normalize_subject({"id": 2, "name": "B", "eps": None})["episodes"])

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
        self.assertIsNone(self.provider.normalize_subject({"id": 4, "name": "A", "eps": 1000000})["episodes"])

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

    @patch("journal.external_media.providers.bangumi.requests.post")
    def test_search_v0_success_uses_compatibility_shape(self, post):
        post.return_value = response({"data": [{"id": 9, "name": "原名", "name_cn": "中文名"}]})
        result = self.provider.search("中文名-v0")
        self.assertEqual(result[0]["id"], 9)
        self.assertEqual(result[0]["external_id"], "9")
        self.assertEqual(result[0]["provider"], "bangumi")

    @patch("journal.external_media.providers.bangumi.requests.get")
    @patch("journal.external_media.providers.bangumi.requests.post")
    def test_search_falls_back_to_legacy_endpoint(self, post, get):
        post.side_effect = requests.ConnectionError("v0 down")
        get.return_value = response({"list": [{"id": 10, "name": "Legacy"}]})
        self.assertEqual(self.provider.search("legacy-fallback")[0]["name"], "Legacy")
        self.assertIn("/search/subject/legacy-fallback", get.call_args.args[0])

    @patch("journal.external_media.providers.bangumi.requests.get")
    @patch("journal.external_media.providers.bangumi.requests.post")
    def test_search_timeout_is_mapped_without_raw_upstream_content(self, post, get):
        post.side_effect = requests.Timeout("secret upstream text")
        get.side_effect = requests.ConnectionError("legacy down")
        with self.assertRaises(ExternalMediaError) as caught:
            self.provider.search("timeout-case")
        self.assertEqual(caught.exception.detail["code"], "provider_timeout")
        self.assertNotIn("secret", str(caught.exception.detail))

    @patch("journal.external_media.providers.bangumi.requests.get")
    @patch("journal.external_media.providers.bangumi.requests.post")
    def test_search_invalid_responses_are_mapped(self, post, get):
        post.return_value = response({"unexpected": []})
        get.return_value = response({"unexpected": []})
        with self.assertRaises(ExternalMediaError) as caught:
            self.provider.search("invalid-response")
        self.assertEqual(caught.exception.detail["code"], "provider_invalid_response")

    @patch("journal.external_media.providers.bangumi.requests.get")
    def test_subject_fetch_uses_cache_and_force_bypasses_it(self, get):
        get.side_effect = [
            response({"id": 11, "name": "First"}), response([]),
            response({"id": 11, "name": "Second"}), response([]),
        ]
        first = self.provider.fetch_subject("11")
        cached = self.provider.fetch_subject("11")
        refreshed = self.provider.fetch_subject("11", force=True)
        self.assertEqual(first["title"], "First")
        self.assertEqual(cached["title"], "First")
        self.assertEqual(refreshed["title"], "Second")
        self.assertEqual(get.call_count, 4)

    @patch("journal.external_media.providers.bangumi.requests.get")
    def test_subject_not_found_is_mapped(self, get):
        get.return_value = response({}, status_code=404)
        with self.assertLogs("journal.external_media.providers.bangumi", level="WARNING") as logs:
            with self.assertRaises(ExternalMediaError) as caught:
                self.provider.fetch_subject("404")
        self.assertEqual(caught.exception.detail["code"], "subject_not_found")
        self.assertIn("provider=bangumi endpoint=subject status=404 error=NotFound", logs.output[0])
        self.assertNotIn("Authorization", logs.output[0])

    @patch("journal.external_media.providers.bangumi.requests.get")
    def test_invalid_json_is_mapped_to_invalid_response(self, get):
        get.return_value = response({})
        get.return_value.json.side_effect = requests.exceptions.JSONDecodeError("bad json", "{", 0)
        with self.assertRaises(ExternalMediaError) as caught:
            self.provider.fetch_subject("405", force=True)
        self.assertEqual(caught.exception.detail["code"], "provider_invalid_response")

    @patch("journal.external_media.providers.bangumi.requests.get")
    def test_persons_failure_keeps_infobox_studio(self, get):
        get.side_effect = [
            response({"id": 12, "name": "A", "infobox": [{"key": "动画制作", "value": "Studio"}]}),
            requests.Timeout("persons timeout"),
        ]
        self.assertEqual(self.provider.fetch_subject("12")["studio"], "Studio")
