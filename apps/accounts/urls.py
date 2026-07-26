from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    ForgotPasswordView,
    LoginView,
    LogoutView,
    MeView,
    OtpResendView,
    OtpVerifyView,
    RegisterView,
    ResetPasswordView,
)

urlpatterns = [
    path("auth/register", RegisterView.as_view()),
    path("auth/otp/verify", OtpVerifyView.as_view()),
    path("auth/otp/resend", OtpResendView.as_view()),
    path("auth/login", LoginView.as_view()),
    path("auth/password/forgot", ForgotPasswordView.as_view()),
    path("auth/password/reset", ResetPasswordView.as_view()),
    path("auth/logout", LogoutView.as_view()),
    path("auth/token/refresh", TokenRefreshView.as_view()),
    path("auth/me", MeView.as_view()),
]
