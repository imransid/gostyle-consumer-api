from rest_framework.throttling import SimpleRateThrottle

from .identifiers import resolve_identifier


class OtpDestinationThrottle(SimpleRateThrottle):
    """Rate-limit OTP sends per destination (phone/email), not per IP.

    The IP-only ScopedRateThrottle let an attacker rotate IPs to SMS-bomb a
    single number; keying on the normalized destination closes that. Falls
    back to no throttling when the request carries no usable identifier — the
    serializer will reject it with a 400 anyway.
    """

    scope = "otp_destination"

    def get_cache_key(self, request, view):
        raw = request.data.get("identifier")
        if not raw:
            return None
        try:
            _, destination = resolve_identifier(raw)
        except ValueError:
            return None
        return self.cache_format % {"scope": self.scope, "ident": destination}
