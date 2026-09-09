"""Run legacy semantic oracles against the separate bounded list/summary APIs.

Only small, complete fixtures use this adapter. Wire shape, pagination and field
continuation have independent direct-response tests in test_public_catalog.
"""
from types import SimpleNamespace

from django.urls import reverse


def public_catalog_response(client, *, public_slug=None, params=None):
    scope = "public-showcase" if public_slug is not None else "public-homepage"
    kwargs = {"public_slug": public_slug} if public_slug is not None else None
    entries = client.get(reverse(scope + "-entries", kwargs=kwargs), params or {})
    summary = client.get(reverse(scope + "-summary", kwargs=kwargs), params or {})
    assert entries.status_code == summary.status_code, (entries.status_code, summary.status_code)
    if entries.status_code != 200:
        return entries
    for response in (entries, summary):
        assert response.data["schema"] == "animemo.public-catalog/v1"
        assert response.data["consistency"] == "live"
        assert len(response.content) <= 524288
    assert entries.data["next_cursor"] is None, "Fixture must be complete; use a page-union oracle for larger inputs"
    assert entries.data["total"] == summary.data["total"]
    assert entries.data["matched_count"] == summary.data["matched_count"]
    return SimpleNamespace(status_code=200, data={"stats": summary.data["stats"], "results": entries.data["results"]})
