from django.urls import path

from .views import MyStatsView


urlpatterns = [
    path("stats/me/", MyStatsView.as_view(), name="stats"),
]
