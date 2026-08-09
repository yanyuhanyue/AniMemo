from django.urls import path

from .views import (
    ExternalAccountAuthorizeView,
    ExternalAccountCallbackView,
    ExternalAccountConnectView,
    ExternalAccountDetailView,
    ExternalAccountImportApplyView,
    ExternalAccountImportPreviewDetailView,
    ExternalAccountImportPreviewView,
    ExternalAccountListView,
    ExternalAccountVerifyView,
)


urlpatterns = [
    path("", ExternalAccountListView.as_view(), name="external-account-list"),
    path("<slug:provider>/connect/", ExternalAccountConnectView.as_view(), name="external-account-connect"),
    path("<slug:provider>/authorize/", ExternalAccountAuthorizeView.as_view(), name="external-account-authorize"),
    path("<slug:provider>/callback/", ExternalAccountCallbackView.as_view(), name="external-account-callback"),
    path("<slug:provider>/verify/", ExternalAccountVerifyView.as_view(), name="external-account-verify"),
    path("<slug:provider>/import-preview/", ExternalAccountImportPreviewView.as_view(), name="external-account-import-preview"),
    path("<slug:provider>/import-preview/<uuid:preview_id>/", ExternalAccountImportPreviewDetailView.as_view(), name="external-account-import-preview-detail"),
    path("<slug:provider>/import-apply/", ExternalAccountImportApplyView.as_view(), name="external-account-import-apply"),
    path("<slug:provider>/", ExternalAccountDetailView.as_view(), name="external-account-detail"),
]
