from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .device_token_views import DeviceTokensView
from .notification_views import NotificationPreferencesView
from .session_views import SessionListView, SessionTokenRefreshView

from .views import (
    CancelDeletionView,
    DeleteAccountView,
    LoginView,
    LogoutView,
    MeView,
    OtpRequestView,
    OtpVerifyView,
    PasswordChangeView,
    PasswordForgotView,
    PasswordResetView,
    PasswordVerifyView,
    RegisterView,
    OtpReSendView,
    UserLookupView,
)

urlpatterns = [
    path("notifications/preferences", NotificationPreferencesView.as_view(), name="notification-preferences"),
    path("auth/otp/request", OtpRequestView.as_view()),
    # Resend is the same operation as request: issue a fresh code.
    path("auth/otp/resend", OtpReSendView.as_view()),
    path("auth/otp/verify", OtpVerifyView.as_view()),
    path("auth/register", RegisterView.as_view()),
    path("auth/login", LoginView.as_view()),
    path("auth/logout", LogoutView.as_view()),
    path("auth/token/refresh", SessionTokenRefreshView.as_view()),
    path("auth/me", MeView.as_view()),
    path("auth/me/delete", DeleteAccountView.as_view()),
    path("auth/me/delete/cancel", CancelDeletionView.as_view()),
    path("auth/password/change", PasswordChangeView.as_view()),
    path("auth/password/forgot", PasswordForgotView.as_view()),
    path("auth/password/verify", PasswordVerifyView.as_view()),
    path("auth/password/reset", PasswordResetView.as_view()),
    path("user/lookup", UserLookupView.as_view()),
    path("me/device-tokens", DeviceTokensView.as_view(), name="device-tokens"),
    path("me/sessions", SessionListView.as_view(), name="sessions"),
]
