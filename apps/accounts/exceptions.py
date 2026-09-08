"""One error shape for the whole API.

Contract agreed with the client (docs: "Auth — Field-Level Error Response"):

    {
      "detail":  "Please correct the highlighted fields.",
      "code":    "validation_error",
      "errors":  [{"field": "destination", "code": "already_registered",
                   "message": "This email is already registered."}]
    }

Rules that matter:
  * Validation failures return 422, not 400, whenever `errors` is non-empty.
  * `detail` is ALWAYS a string. The app renders it directly; an array here
    shows up as [object Object] on screen.
  * `field` is the client's form-field name, or null for errors that belong to
    no single input (rate limits, lockouts). Null-field errors still go INSIDE
    `errors` — the app toasts them.
  * `code` is a contract. The app branches on it, so codes stay stable even
    when the wording changes. Never localise a code.
"""

from rest_framework import status
from rest_framework.views import exception_handler as drf_exception_handler


# Top-level code per status. The client uses this to decide broad behaviour
# before it looks at `errors` at all.
_TOP_LEVEL_CODES = {
    400: "validation_error",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    422: "validation_error",
    429: "rate_limited",
}

_TOP_LEVEL_DETAIL = {
    400: "Please correct the highlighted fields.",
    401: "Authentication required.",
    403: "You do not have permission to do that.",
    404: "Not found.",
    405: "Method not allowed.",
    422: "Please correct the highlighted fields.",
    429: "Too many requests. Please try again later.",
}

# DRF's own names for "this error has no field". Both become null for the
# client, which renders them as a toast rather than highlighting an input.
_NON_FIELD_KEYS = {"detail", "non_field_errors"}


def _flatten(data, field=None):
    """Turn DRF's nested error data into a flat list of {field, code, message}."""
    out = []

    if isinstance(data, dict):
        for key, value in data.items():
            out.extend(_flatten(value, field=key))
        return out

    if isinstance(data, list):
        for item in data:
            out.extend(_flatten(item, field=field))
        return out

    # A leaf. DRF wraps strings in ErrorDetail, which carries .code.
    out.append({
        "field": None if field in _NON_FIELD_KEYS else field,
        "code": getattr(data, "code", "invalid"),
        "message": str(data),
    })
    return out


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        # Not a DRF exception. Let Django's 500 handling take it so the
        # traceback still reaches the logs and Sentry.
        return None

    errors = _flatten(response.data)

    # 400 with field errors is a validation failure; the client expects 422.
    # Other statuses (401, 403, 429) keep their own meaning.
    if response.status_code == status.HTTP_400_BAD_REQUEST and errors:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY

    code = response.status_code

    # A single null-field error IS the summary — repeating a generic sentence
    # above it would say the same thing twice.
    if len(errors) == 1 and errors[0]["field"] is None:
        detail = errors[0]["message"]
    else:
        detail = _TOP_LEVEL_DETAIL.get(code, "Request failed.")

    response.data = {
        "detail": detail,
        "code": _TOP_LEVEL_CODES.get(code, "error"),
        "errors": errors,
    }
    return response