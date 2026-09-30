"""
Swagger in the order the app uses it.

drf-spectacular puts every endpoint under one tag ("v1"), sorted by
address, so a routine's steps end up between group booking and "bookings",
and most endpoints have no title. This hook runs on the finished schema: it
puts each endpoint in a numbered section, in the order a customer meets
them, gives it a numbered step title, and writes the sections and the
endpoints in that same order. Swagger UI shows the schema's order as it is.

DOCS ONLY: nothing here changes what an endpoint does. A new endpoint needs
one line in FLOW; until it has one, it is shown in a last "Not placed yet"
section, so nothing ever disappears from Swagger (and a test says so).
"""

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

NOT_PLACED = "Not placed yet"

# (section, what it is for, ((method, path, step title), ...)), in order.
FLOW = (
    (
        "1. Sign up and log in",
        "A new customer creates an account, a returning one logs in. Then the "
        "phone or email is confirmed with a code. Every other call needs the "
        "access token: press Authorize at the top and paste it.",
        (
            ("POST", "/api/v1/auth/register", "Create an account"),
            ("POST", "/api/v1/auth/login", "Log in, get the tokens"),
            ("POST", "/api/v1/auth/otp/request", "Send a code to confirm my phone or email"),
            ("POST", "/api/v1/auth/otp/resend", "Send the code again"),
            ("POST", "/api/v1/auth/otp/verify", "Confirm the code"),
            ("POST", "/api/v1/auth/token/refresh", "Get a new access token"),
            ("POST", "/api/v1/auth/logout", "Log out"),
        ),
    ),
    (
        "2. Forgot password",
        "Three steps, no login needed: ask for a code, check it, set a new password.",
        (
            ("POST", "/api/v1/auth/password/forgot", "Send a reset code"),
            ("POST", "/api/v1/auth/password/verify", "Check the code, get a reset token"),
            ("POST", "/api/v1/auth/password/reset", "Set a new password"),
        ),
    ),
    (
        "3. My profile",
        "The logged-in customer's own profile and password.",
        (
            ("GET", "/api/v1/auth/me", "Read my profile"),
            ("PATCH", "/api/v1/auth/me", "Change part of my profile"),
            ("PUT", "/api/v1/auth/me", "Replace my profile"),
            ("POST", "/api/v1/auth/password/change", "Change my password"),
        ),
    ),
    (
        "4. Find a salon",
        "The discovery screens: the list, the map and the story rail.",
        (
            ("GET", "/api/v1/discover", "Salons to discover (the list, with filters)"),
            ("GET", "/api/v1/discover/map", "Salons on the map"),
            ("GET", "/api/v1/discover/story", "Salons with a story right now"),
            ("GET", "/api/v1/discover/{id}", "One salon's card"),
        ),
    ),
    (
        "5. A salon's page",
        "Everything on one salon's page. Take salon_id from section 4.",
        (
            ("GET", "/api/v1/salon/{salon_id}", "The salon"),
            ("GET", "/api/v1/salon/{salon_id}/services", "Its services"),
            ("GET", "/api/v1/salon/{salon_id}/service/{service_id}", "One of its services, for the detail screen"),
            ("GET", "/api/v1/salon/{salon_id}/stylists", "Its stylists (for some services, if given)"),
            ("GET", "/api/v1/salon/{salon_id}/packages", "Its packages"),
            ("GET", "/api/v1/salon/{salon_id}/products", "Its shop"),
            ("GET", "/api/v1/salon/{salon_id}/stories", "Its stories"),
        ),
    ),
    (
        "6. Services and stylists",
        "Lookups the booking screens use.",
        (
            ("GET", "/api/v1/services", "Services"),
            ("GET", "/api/v1/services-details", "Service details, from their ids"),
            ("GET", "/api/v1/stylists", "Stylists"),
        ),
    ),
    (
        "7. Favourites",
        "The heart on a salon.",
        (
            ("GET", "/api/v1/favourite", "My saved salons"),
            ("POST", "/api/v1/favourite", "Save or unsave a salon (the heart)"),
        ),
    ),
    (
        "8. Book one visit",
        "Find a free time, book it, read it, record the payment.",
        (
            ("GET", "/api/v1/booking/nearest-available/{salon_id}", "The nearest free time"),
            ("POST", "/api/v1/booking", "Book it"),
            ("GET", "/api/v1/booking/{booking_id}", "Read one booking"),
            ("PATCH", "/api/v1/booking/{booking_id}", "Record the payment"),
        ),
    ),
    (
        "9. Book for a group",
        "Several people in one booking. Read it back with section 8, step 3.",
        (
            ("GET", "/api/v1/user/lookup", "Find a friend by phone or email"),
            ("POST", "/api/v1/booking/group-availability", "Which times fit the whole party"),
            ("POST", "/api/v1/booking/group", "Book the whole party"),
            ("POST", "/api/v1/booking/{booking_id}/cancel", "Cancel the whole party"),
        ),
    ),
    (
        "10. Routine (repeat booking)",
        "The same visit again and again: daily, weekly, every 2 weeks, monthly "
        "or on chosen days. Always preview first, with dry_run: true.",
        (
            ("POST", "/api/v1/booking/series", "Preview, then book a routine"),
            ("GET", "/api/v1/booking/series/{series_id}", "The routine hub"),
            ("PATCH", "/api/v1/booking/series/{series_id}", "Change it: skip, move, add, pause, resume"),
            ("POST", "/api/v1/booking/series/{series_id}/cancel", "Cancel it (preview the refund first)"),
            ("POST", "/api/v1/booking/routine-preview", "App contract: preview a routine (ROUTINE_CONTRACT_V1)"),
            ("POST", "/api/v1/booking/routine", "App contract: book a routine (ROUTINE_CONTRACT_V1)"),
            (
                "PATCH",
                "/api/v1/booking/{booking_id}/sessions/{session_id}",
                "App contract: move one session (ROUTINE_CONTRACT_V1)",
            ),
        ),
    ),
    (
        "11. My bookings",
        "The three tabs.",
        (
            ("GET", "/api/v1/bookings", "Upcoming, Recurring or Archive"),
        ),
    ),
    (
        "12. Uploads",
        "Send files, get their links back.",
        (
            ("POST", "/api/v1/upload-files", "Upload files"),
        ),
    ),
    (
        "13. Retired",
        "Kept only so an old app gets a clear answer. Do not use.",
        (
            ("GET", "/api/v1/salons/", "Gone: use section 4"),
        ),
    ),
)


def order_by_flow(result, generator=None, request=None, public=None):
    """
    drf-spectacular postprocessing hook (SPECTACULAR_SETTINGS
    POSTPROCESSING_HOOKS). Rewrites only `tags`, each operation's `tags`
    and `summary`, and the order of `paths`; everything else is kept as is.
    """
    paths = result.get("paths") or {}
    ordered = {}
    tags = []
    placed = set()
    for section, about, steps in FLOW:
        tags.append({"name": section, "description": about})
        for number, (method, path, title) in enumerate(steps, start=1):
            op = (paths.get(path) or {}).get(method.lower())
            if op is None:
                continue
            op["tags"] = [section]
            op["summary"] = f"Step {number}: {title}"
            ordered.setdefault(path, {})[method.lower()] = op
            placed.add((method.lower(), path))

    leftover = False
    for path, item in paths.items():
        for key, value in item.items():
            if key in METHODS and (key, path) not in placed:
                value["tags"] = [NOT_PLACED]
                leftover = True
            if key not in METHODS or (key, path) not in placed:
                ordered.setdefault(path, {})[key] = value
    if leftover:
        tags.append({
            "name": NOT_PLACED,
            "description": "New endpoints that config/openapi_flow.py does not place yet.",
        })

    result["paths"] = ordered
    result["tags"] = tags
    return result
