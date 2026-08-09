from django.urls import path

from .views import ExternalCollectionSyncApplyView, ExternalCollectionSyncPreviewView

urlpatterns = [
    path(
        "external-sync/providers/<slug:provider>/entries/<int:entry_id>/preview/",
        ExternalCollectionSyncPreviewView.as_view(),
        name="external-collection-sync-preview",
    ),
    path(
        "external-sync/providers/<slug:provider>/entries/<int:entry_id>/apply/",
        ExternalCollectionSyncApplyView.as_view(),
        name="external-collection-sync-apply",
    ),
]
