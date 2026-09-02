from django.core.exceptions import RequestDataTooBig
from django.test import SimpleTestCase, override_settings
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory


class RequestSizeSecurityTests(SimpleTestCase):
    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10)
    def test_oversized_json_body_is_rejected_before_parsing(self):
        request = Request(
            APIRequestFactory().post(
                "/",
                b'{"qwerty": "uiop"}',
                content_type="application/json",
            )
        )
        request.parsers = (JSONParser(),)

        with self.assertRaises(RequestDataTooBig):
            request.data

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10)
    def test_oversized_urlencoded_body_is_rejected_before_parsing(self):
        request = Request(
            APIRequestFactory().post(
                "/",
                b"qwerty=uiop&asdf=ghjkl",
                content_type="application/x-www-form-urlencoded",
            )
        )
        request.parsers = (FormParser(),)

        with self.assertRaises(RequestDataTooBig):
            request.data
