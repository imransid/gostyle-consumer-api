from django.urls import path
from .device_token_views import DeviceTokensView
from .notification_views import NotificationPreferencesView
from .session_views import SessionDetailView, SessionsView, SessionTokenRefreshView
from .two_factor_views import (
    AuthenticatorConfirmView,
    AuthenticatorView,
    SecurityView,
    TwoFactorSwitchView,
    VerifySendView,
    VerifyView,
)

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
    path("me/sessions", SessionsView.as_view(), name="sessions"),
    path("me/sessions/<str:session_id>", SessionDetailView.as_view(), name="session-detail"),
    path("me/security", SecurityView.as_view(), name="security"),
    path("me/security/verify/send", VerifySendView.as_view(), name="security-verify-send"),
    path("me/security/verify", VerifyView.as_view(), name="security-verify"),
    path("me/security/two-factor", TwoFactorSwitchView.as_view(), name="security-two-factor"),
    path("me/security/authenticator", AuthenticatorView.as_view(), name="security-authenticator"),
    path(
        "me/security/authenticator/confirm",
        AuthenticatorConfirmView.as_view(),
        name="security-authenticator-confirm",
    ),
]
