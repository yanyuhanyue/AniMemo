from django.urls import path

from .views import WatchHistoryCollectionView, WatchHistoryDetailView


urlpatterns = [
    path(
        "entries/<int:entry_id>/watch-history/",
        WatchHistoryCollectionView.as_view(),
        name="watch-history-collection",
    ),
    path(
        "entries/<int:entry_id>/watch-history/<int:record_id>/",
        WatchHistoryDetailView.as_view(),
        name="watch-history-detail",
    ),
]
