from django.urls import include, path

from journal.auth_views import CookieTokenRefreshView, EmailTokenObtainPairView


# Canonical and legacy prefixes mount this single route table. Views, serializers,
# permissions and business behavior therefore cannot drift between API versions.
urlpatterns = [
    path("token/", EmailTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("", include("site_config.urls")),
    path("", include("journal.urls")),
]
