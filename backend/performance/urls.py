from django.urls import path

from .views import IsolatedProviderLatencyView

urlpatterns = [
    path("provider-latency/", IsolatedProviderLatencyView.as_view(), name="isolated-provider-latency"),
]
