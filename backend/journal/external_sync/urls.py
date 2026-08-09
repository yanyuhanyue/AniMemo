from django.urls import path

from .views import ExternalCollectionSyncPreviewView

urlpatterns = [
    path(
        "external-sync/providers/<slug:provider>/entries/<int:entry_id>/preview/",
        ExternalCollectionSyncPreviewView.as_view(),
        name="external-collection-sync-preview",
    ),
]
