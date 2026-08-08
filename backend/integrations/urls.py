from django.urls import path

from .views import (
    ActionsView,
    BindingDetailView,
    BindingsView,
    ConnectionsView,
    EventsAckView,
    EventsView,
    PairConsumeView,
    PairingCodesView,
)


app_name = "integrations"

urlpatterns = [
    path("connections/", ConnectionsView.as_view(), name="connections"),
    path("pairing-codes/", PairingCodesView.as_view(), name="pairing-codes"),
    path("bindings/", BindingsView.as_view(), name="bindings"),
    path("bindings/<int:binding_id>/", BindingDetailView.as_view(), name="binding-detail"),
    path("pair/consume/", PairConsumeView.as_view(), name="pair-consume"),
    path("actions/", ActionsView.as_view(), name="actions"),
    path("events/", EventsView.as_view(), name="events"),
    path("events/ack/", EventsAckView.as_view(), name="events-ack"),
]
