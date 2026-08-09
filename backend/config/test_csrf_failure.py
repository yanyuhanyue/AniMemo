from django.test import Client, SimpleTestCase


class ApiCsrfFailureTests(SimpleTestCase):
    def test_api_csrf_failure_uses_stable_code(self):
        response = Client(enforce_csrf_checks=True).post("/api/token/refresh/", {})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_failed")
