from django.urls import path

from .views import ExternalMediaSearchView, ExternalMediaSubjectView


urlpatterns = [
    path(
        "external-media/providers/<slug:provider>/search/",
        ExternalMediaSearchView.as_view(),
        name="external-media-search",
    ),
    path(
        "external-media/providers/<slug:provider>/subjects/<str:external_id>/",
        ExternalMediaSubjectView.as_view(),
        name="external-media-subject",
    ),
]
