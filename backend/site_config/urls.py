from django.urls import path

from .views import InstallationSetupView, InstallationStatusView


urlpatterns = [
    path("setup/status/", InstallationStatusView.as_view(), name="setup-status"),
    path("setup/", InstallationSetupView.as_view(), name="setup-complete"),
]
