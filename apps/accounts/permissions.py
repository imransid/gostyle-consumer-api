from rest_framework.permissions import BasePermission


class IsVerified(BasePermission):
    """Requires a caller who has proved their phone or email with an OTP.

    Registration creates the account and hands back tokens immediately, so an
    authenticated caller is NOT necessarily a verified one. `account_verified`
    is set by `services.verify_otp_for_user` the first time a code is accepted;
    until then the member is real, logged in, and unproven.

    `IsAuthenticated` alone does not express that. This class does, and it
    subsumes it: an anonymous caller fails the `is_authenticated` check here
    too, so use it instead of `IsAuthenticated`, not alongside it.

        permission_classes = [IsVerified]

    WHEN TO APPLY IT
    ----------------
    Use it on anything that acts on the outside world in the member's name, or
    that a throwaway account could abuse:

      * creating or cancelling a booking (a salon holds a real chair for this)
      * anything touching payments, deposits or refunds
      * writing a review, which is published under the member's name
      * registering a push device

    WHEN NOT TO APPLY IT
    --------------------
    Never put it on the endpoints an unverified member needs in order to BECOME
    verified, or they are locked out of their own account:

      * `POST /auth/otp/request` and `/auth/otp/verify` — these are the way out
        of the unverified state. They are already `IsAuthenticated`, and they
        act on the caller's own contact on file rather than a supplied one.
      * `GET`/`PATCH /auth/me` — the app reads the profile on launch to decide
        which screen to show, including the "verify your account" prompt. A 403
        here would leave the app unable to tell a new member what to do next.

    Read-only browsing (salon discovery, profiles) does not need it either;
    those endpoints are about what the app may show, not what the member may do.

    Login already refuses an unverified contact (see `LoginView`), so this class
    matters for the window between registering and verifying, when the caller
    holds valid tokens from `register()` and has not yet used a code.
    """

    message = "Verify your account to use this feature."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and user.account_verified)
