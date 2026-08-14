from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
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
)

urlpatterns = [
    path("auth/otp/request", OtpRequestView.as_view()),
    # Resend is the same operation as request: issue a fresh code.
    path("auth/otp/resend", OtpRequestView.as_view()),
    path("auth/otp/verify", OtpVerifyView.as_view()),
    path("auth/register", RegisterView.as_view()),
    path("auth/login", LoginView.as_view()),
    path("auth/logout", LogoutView.as_view()),
    path("auth/token/refresh", TokenRefreshView.as_view()),
    path("auth/me", MeView.as_view()),
    path("auth/password/change", PasswordChangeView.as_view()),
    path("auth/password/forgot", PasswordForgotView.as_view()),
    path("auth/password/verify", PasswordVerifyView.as_view()),
    path("auth/password/reset", PasswordResetView.as_view()),
]
