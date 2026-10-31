from datetime import timedelta
from pathlib import Path
import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "apps.accounts",
    "drf_spectacular",
    "apps.salons", 
    "apps.platform_data",
    "apps.uploads",
    "apps.booking_data",
    "apps.review_data",
    "apps.push_data",
    "apps.notifications",
]

MIDDLEWARE = [
    # First, so that responses returned by the middleware below it (the HTTPS
    # redirect, CommonMiddleware's 301s) still carry X-Request-ID.
    "config.observability.RequestIDMiddleware",
    'django.middleware.security.SecurityMiddleware',
    "whitenoise.middleware.WhiteNoiseMiddleware",
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}



# File uploads (apps/uploads). Same bucket and same variable names as
# gostyle-platform, so the server's .env lines can be copied from there.
# Empty means "no S3": files go to MEDIA_ROOT on this disk (local dev only).
AWS_BUCKET_NAME = env("AWS_BUCKET_NAME", default="")
AWS_REGION = env("AWS_REGION", default="")

# boto3 reads AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY from the
# environment by itself, so they are not read here.
S3_STORAGE = {
    "BACKEND": "storages.backends.s3.S3Storage",
    "OPTIONS": {
        "bucket_name": AWS_BUCKET_NAME,
        "region_name": AWS_REGION,
        # Our own top-level folder. Never "tenants/": the platform treats
        # every key under it as a salon's.
        "location": "customers",
        # Plain public URL, in the same shape the platform builds.
        "custom_domain": f"{AWS_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com",
        "querystring_auth": False,
    },
}

if AWS_BUCKET_NAME:
    STORAGES["default"] = S3_STORAGE

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


AUTH_USER_MODEL = "accounts.ConsumerAccount"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST", default="127.0.0.1"),
        "PORT": env("DB_PORT", default="5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {
            "options": f"-c search_path={env('DB_SCHEMA', default='consumer')},public"
        },
    }
}

# Other services' databases, for the admin only (apps/booking_data,
# apps/review_data, apps/push_data; config/db_router.py). Each one is off until
# its variable is set, and the admin then leaves that app out. The session is
# read-only and the database user can only SELECT, so nothing here ever writes.
# No persistent connections: the admin opens them rarely.
READ_ONLY_DB = {
    "CONN_MAX_AGE": 0,
    "OPTIONS": {
        "options": "-c default_transaction_read_only=on",
        "connect_timeout": 5,
    },
}

# booking-api's database is on the same server as ours, so it reuses our login
# and only the database name changes.
if BOOKING_DB_NAME := env("BOOKING_DB_NAME", default=""):
    DATABASES["booking"] = {
        **DATABASES["default"],
        **READ_ONLY_DB,
        "NAME": BOOKING_DB_NAME,
    }

# review-service and push-notification-service each run their own Postgres:
# postgres://<read-only user>:<password>@<host>:5432/<db>
for alias in ("review", "push"):
    if url := env(f"{alias.upper()}_DATABASE_URL", default=""):
        DATABASES[alias] = {**environ.Env.db_url_config(url), **READ_ONLY_DB}

DATABASE_ROUTERS = ["config.db_router.ReadOnlyDataRouter"]

# DATABASES = {
#     "default": {
#         "ENGINE": "django.db.backends.postgresql",
#         "NAME": env("DB_NAME"),
#         "USER": env("DB_USER"),
#         "PASSWORD": env("DB_PASSWORD"),
#         "HOST": env("DB_HOST"),
#         "PORT": env("DB_PORT", default="5432"),
#         "CONN_MAX_AGE": 60,
#         "OPTIONS": {
#             "sslmode": "require",
#         },
#     }
# }

SPECTACULAR_SETTINGS = {
    "TITLE": "GoStyle Customer API",
    "DESCRIPTION": (
        "The sections are numbered in the order a customer uses the app, and so "
        "are the steps inside each one. Start with section 1: log in, then press "
        "Authorize and paste the access token."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_SETTINGS": {"persistAuthorization": True},
    # Swagger in the order the app uses it (config/openapi_flow.py). The first
    # hook is drf-spectacular's own default, kept as it is.
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "config.openapi_flow.order_by_flow",
    ],
}

# Cache / rate-limit store. The OTP flow enforces its limits here before it
# touches Postgres. Production points this at Redis; local dev and tests use
# in-memory (see config.settings.local).
REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")

# gostyle-booking-api, which owns bookings and their database. This service
# never writes a booking row; it forwards to that service and hands back what
# it answers (apps/salons/booking_api.py).
#
# The base URL carries NO /v1: that prefix is the booking service's own
# (`setGlobalPrefix('v1')`), so the client appends it per path, and a future
# /v2 does not need every deployment's env file edited. The default is the
# Swarm service name, which is what production resolves anyway — the variable
# exists for local development and for the day the service moves.
BOOKING_API_URL = env("BOOKING_API_URL", default="http://gostyle-booking_api:3851")

# A customer is waiting on this call, so it fails fast rather than holding a
# gunicorn worker open. Creating a booking is one transaction over there.
BOOKING_API_TIMEOUT = env.int("BOOKING_API_TIMEOUT", default=10)


PUSH_API_URL = env("PUSH_API_URL", default="http://push-app:3351")
PUSH_API_KEY = env("PUSH_API_KEY", default="")
PUSH_API_TIMEOUT = env.int("PUSH_API_TIMEOUT", default=3)

# The key internal services present to ConsumerDirectory on the gRPC server
# (metadata x-internal-key). booking-api sends the same value, under the same
# name, when it looks up a customer's email for a reminder. EMPTY REFUSES
# EVERY CALL: the answer is a person's email address.
INTERNAL_GRPC_KEY = env("INTERNAL_GRPC_KEY", default="")

# Group bookings through booking-api's mobile route, POST
# /v1/mobile-booking/group (docs/GROUP_BOOKING_PLAN.md): one call, the party
# saved to pay at the salon, products allowed, read back by group id, shown
# as GROUP in /bookings and cancelled together. OFF keeps the old hold and
# confirm path exactly as it was. Turn it on only where booking-api runs with
# MOBILE_GROUP_BOOKING=true, or every group booking answers 404.
GROUP_BOOKING_V2 = env.bool("GROUP_BOOKING_V2", default=False)

# Routine (series) bookings through booking-api's mobile routes, POST and GET
# /v1/mobile-booking/series (docs/SERIES_BOOKING_AUDIT.md, E.3). OFF answers
# 404 on /api/v1/booking/series and changes nothing else. Turn it on only
# where booking-api runs with MOBILE_SERIES_BOOKING=true.
SERIES_BOOKING_V1 = env.bool("SERIES_BOOKING_V1", default=False)

# A routine create books up to 6 visits one by one, so it waits longer than
# one booking does: measured 2.4 to 8.7 seconds for 6 visits on a laptop
# (2026-09-26). Kept under gunicorn's --timeout 60, which would kill the
# worker first and answer the app with nothing at all.
SERIES_BOOKING_CREATE_TIMEOUT = env.int("SERIES_BOOKING_CREATE_TIMEOUT", default=45)

# The app team's routine contract (docs/routine-booking-fe-contract.md and
# docs/ROUTINE_FE_CONTRACT_AUDIT.md): POST /api/v1/booking/routine-preview,
# POST /api/v1/booking/routine and PATCH /api/v1/booking/{id}/sessions/{id},
# next to the /booking/series routes, which do not change. OFF answers 404 on
# the three and changes nothing else. Turn it on only where booking-api runs
# with MOBILE_ROUTINE_CONTRACT=true (and MOBILE_SERIES_BOOKING=true).
ROUTINE_CONTRACT_V1 = env.bool("ROUTINE_CONTRACT_V1", default=False)

# A customer cancels a SINGLE booking through POST /api/v1/booking/{id}/cancel,
# forwarded to booking-api's POST /v1/bookings/{id}/cancel, and moves one
# through POST /api/v1/booking/{id}/reschedule: a hold (POST /v1/holds), then
# POST /v1/bookings/{id}/reschedule (single_views.py). OFF, the cancel route is
# exactly the group cancel it was, a single booking's id is 404 there as
# before, and the reschedule route is 404 for every id.
SINGLE_BOOKING_ACTIONS_V1 = env.bool("SINGLE_BOOKING_ACTIONS_V1", default=False)

# How long that cancel waits for its lookup (the caller's Upcoming shelf)
# before treating the id as not single and going on to the group cancel. Short
# on purpose: the lookup is optional, and with booking-api down a cancel must
# not wait BOOKING_API_TIMEOUT twice before its 503. The reschedule uses the
# same lookup, and an id it does not find there is 404.
SINGLE_LOOKUP_TIMEOUT = env.int("SINGLE_LOOKUP_TIMEOUT", default=3)

# Self check-in: POST /api/v1/booking/{id}/check-in says "I am here" and GET
# reads the desk's answer, forwarded to booking-api's
# /v1/bookings/{id}/check-in-request (check_in_views.py). booking-api has its
# own flag of the same name; both must be on. OFF here, both are the 404 this
# path was before, and booking-api is not called.
SELF_CHECK_IN_V1 = env.bool("SELF_CHECK_IN_V1", default=False)

# Chair scanning, inside self check-in: Scan the chair is offered only while
# this is on (`can_scan_chair`), and a POST with `chair_token` while it is off
# is refused with 409 BOOKING_CHAIR_SCAN_OFF, never raised without the chair
# (check_in_views.py). Wait for Staff does not depend on it. Turn it on only
# once platform's chair cards and booking-api's chair check are live
# (docs/MOBILE_SELF_CHECK_IN_FE.md §12.5). SELF_CHECK_IN_V1 off turns it off too.
CHAIR_SCAN_V1 = env.bool("CHAIR_SCAN_V1", default=False)

# Group check-in: POST /api/v1/booking/group/{group_id}/check-in says "we are
# here" for every member of an app party, and GET reads each lane's answer,
# forwarded to booking-api's /v1/mobile-booking/group/{group_id}/check-in
# (group_check_in_views.py). The booker only. booking-api's
# MOBILE_GROUP_CHECK_IN, SELF_CHECK_IN_V1 and MOBILE_GROUP_BOOKING must be on
# too. OFF here, both are the 404 this path was before, booking-api is not
# called, and the GROUP row offers no button. SELF_CHECK_IN_V1 off turns it
# off too.
GROUP_CHECK_IN_V1 = env.bool("GROUP_CHECK_IN_V1", default=False)

# Group self check-in: a member of an app party says "I am here" for their own
# lane, with the single check-in route (POST /api/v1/booking/{id}/check-in,
# check_in_views.py) and the lane's booking_id from the party read. On, the
# member's own row of the party read (is_me true) carries can_check_in and
# check_in_opens_at. booking-api's MOBILE_GROUP_SELF_CHECK_IN must be on too
# (it gives the row is_me and booking_id), and SELF_CHECK_IN_V1 here (the
# route the member uses). OFF here, this service adds nothing to the member
# rows: no can_check_in, check_in_opens_at or can_scan_chair. It does not
# hide anything either: while booking-api's switch is on, is_me and
# booking_id still come through on the party read as booking-api sent them,
# and the single check-in route still takes a member's lane, because
# SELF_CHECK_IN_V1 alone decides that route.
GROUP_SELF_CHECK_IN_V1 = env.bool("GROUP_SELF_CHECK_IN_V1", default=False)

# A party's Cancel button: can_cancel on a GROUP row of My Bookings is the
# booker's alone, and looks at the party's members, not only its status. The
# party cancel answers anyone but the booker 404, so a row whose is_booker is
# not true (booking-api's, sent with its MOBILE_GROUP_CHECK_IN) has no button.
# It is all or nothing, and booking-api refuses it (409 cannot_cancel) once
# any lane is checked in, so the button must not be offered then either. On,
# each booker's GROUP row that the window would let cancel is read once (GET
# /v1/mobile-booking/group/{id}, within SINGLE_LOOKUP_TIMEOUT), and
# can_cancel is false when any member row is CHECKED_IN or COMPLETED, or the
# read does not answer. Needed before booking-api's MOBILE_GROUP_LIVE_STATUS
# goes on (a party then reads confirmed while any lane is). OFF here,
# can_cancel is exactly what it was and no party is read.
GROUP_CANCEL_LANES_V1 = env.bool("GROUP_CANCEL_LANES_V1", default=False)
# The Packages tab on the platform's own rules (docs/PACKAGE_DETAIL_AUDIT.md,
# F1 to F4): a package is sold at every branch unless that branch's row says
# no, the tab shows its published content (not the owner's draft), an item's
# variant sets its price and minutes, and a package is shown only when every
# service in it can be booked. GET /salon/{id}/package/{id} always uses these
# rules, so ON the tab and the detail agree. OFF here, the tab answers
# exactly what it did, and with PACKAGE_BOOKING_V1 on every bundle's
# can_book is false (it shows the draft's price, not the one charged).
PACKAGES_LIVE_RULES_V1 = env.bool("PACKAGES_LIVE_RULES_V1", default=False)

# Booking a package as one visit at its price (docs/BOOKING_CREATE_API.md,
# "Booking a package"; option A in docs/PACKAGE_DETAIL_AUDIT.md). ON, each
# Packages tab bundle and the Package Details answer carry `can_book`:
# booking-api's rule for selling it (packages.can_book), on the published
# content. Except on the tab's old rules (PACKAGES_LIVE_RULES_V1 off), where
# it is false on every bundle: those show the draft row's price, not the
# published one booking-api charges, so the tab offers Book only with both
# on. And GET /booking/nearest-available takes a package id among
# service_ids, searching for its parts (each as many times as the package
# holds it), or answers 422 package_not_bookable. And a party member may hold
# a package as a line of `services` (POST /booking/group) or among
# `service_ids` (its availability): this service's checks count it as its
# parts (foreign_id, the stylist, the party's length and notice) or answer
# 422 package_not_bookable, and booking-api is sent the line as it came.
# And booking-api's own pick of a stylist for a member with no chosen one
# is checked, since it checks no skills (a package is often more than one
# person's work): a group plan or hold giving a member someone who cannot do
# all of their services is asked again with the stylists shared out, or the
# start is unavailable (slot_taken on create); after the fact, logged, on
# GROUP_BOOKING_V2 (docs/BOOKING_GROUP_API.md, section 5).
# OFF here, the key is absent, a package id there is answered as before (as
# no service of the salon), and the screens and the party routes answer
# exactly what they did. Turn it on only where booking-api runs with
# PACKAGES_FROM_PLATFORM=true: without it booking-api sells no package, and
# every one answers 422 unknown_service.
PACKAGE_BOOKING_V1 = env.bool("PACKAGE_BOOKING_V1", default=False)

# The slot search's minutes are the ones booking-api books. booking-api books
# a service for its catalogue duration_minutes and refuses a create whose
# end_time is anything else; the slot search (GET /booking/nearest-available)
# and a party's time checks (POST /booking/group-availability and
# /booking/group) used the stages' sum instead where a service has stages, so
# an offer could end 7 minutes before the booking would (53 against 60), and
# a start near closing or before another visit was offered that booking-api
# refuses. ON, the catalogue's minutes win, stages only for a service with
# none. OFF here, the minutes are exactly what they were. Nothing in
# booking-api changes.
SLOT_MINUTES_AS_BOOKED_V1 = env.bool("SLOT_MINUTES_AS_BOOKED_V1", default=False)

# A package's saving sent on create as a discount (amount_without_tax: 200,
# discount: 20 for a 180 package), moved back into the price here before
# booking-api sees it. booking-api checks each figure and refuses that body
# with 422 amount_mismatch, though the money adds up. ON, POST /booking with
# a discount and a package of this salon in it asks booking-api's quote, and
# only when the app's tax, total and due are the quote's and the extra
# discount is exactly the packages' savings are amount_without_tax and
# discount set to the quote's; every other body is forwarded as it came.
# OFF here, create forwards exactly what it did. Nothing in booking-api
# changes (views._mend_package_create).
PACKAGE_SAVING_AS_DISCOUNT_V1 = env.bool("PACKAGE_SAVING_AS_DISCOUNT_V1", default=False)

# A package's create with an end_time of the app's own (30 minutes for a
# 60-minute package): booking-api refuses any end_time that is not
# start_time plus every part's minutes ("These services run 60 minutes, so
# the visit ends at ..."). ON, POST /booking with a package of this salon
# and an end_time asks booking-api's quote, and when the quote starts at the
# app's start_time and ends elsewhere, its end_time replaces the app's. The
# start is never moved, and booking-api still checks the stylist is free for
# the whole visit. OFF here, create forwards exactly what it did. Nothing in
# booking-api changes (views._mend_package_create).
PACKAGE_END_TIME_FROM_QUOTE_V1 = env.bool("PACKAGE_END_TIME_FROM_QUOTE_V1", default=False)

# Where you're signed in (docs/DEVICE_SESSIONS_API.md): GET /me/sessions,
# DELETE /me/sessions/{id} and DELETE /me/sessions. ON, a login or register
# opens a session for the device the app's X-Device-* headers describe, and
# so does the refresh of a token issued before; both tokens carry its id as
# `sid`. A request, a refresh or booking-api's VerifyConsumer with an ended
# session's token answers 401 session_revoked (VerifyConsumer: not valid).
# An access token with no sid answers the 401 of an invalid token, so the
# app refreshes it: the first minutes after turning this on, every phone
# refreshes once. Logout ends the current session, a password reset all of
# them, and ending one removes the push tokens it registered. OFF here, the
# three routes are 404, tokens carry no sid and nothing is checked: login,
# register, refresh, logout and the reset are exactly what they were.
# Turned off after being on, a signed-out device's access token works again
# until it expires; its refresh token stays revoked.
DEVICE_SESSIONS_V1 = env.bool("DEVICE_SESSIONS_V1", default=False)

# 2-step verification and the authenticator app (docs/TWO_FACTOR_API.md):
# GET /me/security and the /me/security/* calls, POST /auth/2fa/send and
# /auth/2fa/verify, and the sign-in challenge on POST /auth/login. Works only
# with DEVICE_SESSIONS_V1 on too: a verification token belongs to a session.
# OFF (either one), the new routes are 404 and login is what it was, even for
# an account that turned 2-step on while it was ON.
TWO_FACTOR_V1 = env.bool("TWO_FACTOR_V1", default=False)

# The Notification Center (docs/NOTIFICATION_CENTER_API.md): GET
# /notifications and its read, read-all and unread-count calls, and the
# sign-in alert's push going through it (kept in the center, sent by the
# notification_worker service). OFF, those routes are 404 and the sign-in
# push goes straight to the push service, as before. Turn it on only where
# the worker runs, or items are kept but no push goes out.
NOTIFICATION_CENTER_V1 = env.bool("NOTIFICATION_CENTER_V1", default=False)

# The key authenticator secrets are encrypted with (AES-GCM): 32 random bytes,
# base64. docs/TWO_FACTOR_SERVER.md says how to make one. Empty or not a key:
# the authenticator calls answer 503 and log it, email and phone codes still
# work. Never change it while secrets exist: they would not decrypt.
TWO_FACTOR_ENCRYPTION_KEY = env("TWO_FACTOR_ENCRYPTION_KEY", default="")

# What host nginx sends as X-Proxy-Secret (nginx/api.gostyle.uk.conf), so a
# session's IP is read from X-Forwarded-For only for a request that really
# came through nginx (apps/accounts/sessions.client_ip). Empty: never, and
# every session's IP is the socket address, which has no place.
PROXY_SECRET = env("PROXY_SECRET", default="")

# The DB-IP City Lite .mmdb file a session's location is read from
# (apps/accounts/geoip.py; scripts/update-geoip.sh downloads it, free). Empty,
# or a file that is not there: location stays null and nothing fails.
# docs/DEVICE_SESSIONS_SERVER.md says how to install and refresh it.
GEOIP_CITY_DB = env("GEOIP_CITY_DB", default="")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "apps.accounts.exceptions.api_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # simplejwt's, and an ended session's token is refused (DEVICE_SESSIONS_V1).
        "apps.accounts.authentication.SessionJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "config.pagination.ClientPageNumberPagination",
    # The DEFAULT page size. Clients override it per request with ?page_size=,
    # up to the cap in that class.
    "PAGE_SIZE": 15,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=7),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    # Invalidate the old refresh token once it is rotated, and let logout
    # blacklist a refresh token (requires the token_blacklist app).
    "BLACKLIST_AFTER_ROTATION": True,
    "ISSUER": "gostyle-consumer",
    "AUDIENCE": "gostyle-consumer",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "consumer_id",
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

# Mirrors the "Create New Password" screen: 8+ chars, number, uppercase, symbol.
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'apps.accounts.validators.UppercaseValidator',
    },
    {
        'NAME': 'apps.accounts.validators.NumberValidator',
    },
    {
        'NAME': 'apps.accounts.validators.SymbolValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'


# Logging. Everything goes to stdout as one JSON object per line; the container
# runtime captures it and Grafana Alloy ships it to Loki. See
# config/observability.py for the formatter and docs/OBSERVABILITY.md for the
# rest of the pipeline. DJANGO_LOG_FORMAT=plain swaps in human-readable output
# for anyone tailing a shell (config.settings.local does this by default).
LOG_LEVEL = env("DJANGO_LOG_LEVEL", default="INFO").upper()
LOG_FORMAT = env("DJANGO_LOG_FORMAT", default="json")

LOGGING = {
    "version": 1,
    # Third-party libraries configure loggers at import time; leave them alone.
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "config.observability.RequestIDFilter"},
    },
    "formatters": {
        "json": {"()": "config.observability.JsonFormatter"},
        "plain": {
            "format": "{levelname:<8} {name} [{request_id}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            # One stream, so lines cannot interleave between stdout and stderr.
            "stream": "ext://sys.stdout",
            "filters": ["request_id"],
            "formatter": LOG_FORMAT,
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"level": LOG_LEVEL},
        # Guards against DJANGO_LOG_LEVEL=DEBUG turning every request into a
        # flood of SQL and autoreload chatter. Raise these deliberately instead.
        "django.db.backends": {"level": "INFO"},
        "django.utils.autoreload": {"level": "INFO"},
        # A wrong Host header is an internet background-noise event, not a
        # 500-a-minute alarm.
        "django.security.DisallowedHost": {"level": "ERROR"},
    },
}


# Email (code emails and security notices), over the SendGrid Mail Send HTTPS
# API. There is no SMTP path and nothing falls back to one: a send SendGrid
# refuses raises (apps/accounts/sendgrid_backend.py). The names are the ones
# gostyle-platform and gostyle-booking-api read, so one SendGrid account and
# one authenticated domain (gostyle.uk) serve all three. See docs/EMAIL.md.
#
# `.strip() or default`, not env(default=...): compose expands an unset ${VAR}
# to "", which env() hands back as-is.
SENDGRID_API_KEY = env("SENDGRID_API_KEY", default="").strip()
SENDGRID_FROM_EMAIL = env("SENDGRID_FROM_EMAIL", default="").strip() or "noreply@gostyle.uk"
SENDGRID_FROM_NAME = env("SENDGRID_FROM_NAME", default="").strip() or "GoStyle"

EMAIL_BACKEND = env(
    "EMAIL_BACKEND", default="apps.accounts.sendgrid_backend.SendGridEmailBackend"
)
# Kept aligned with the SendGrid sender: SendGrid refuses any From that is not
# on the authenticated domain, so a separate default here could only be wrong.
DEFAULT_FROM_EMAIL = f"{SENDGRID_FROM_NAME} <{SENDGRID_FROM_EMAIL}>"


# OTP delivery. "console" logs codes to stdout (local dev); any other value
# selects the real senders, and the destination type then picks WhatsApp
# (phone) vs email. Phone OTP is WhatsApp only, no SMS fallback.
OTP_SENDER = env("OTP_SENDER", default="console")

# WhatsApp Cloud API (Meta Graph API) for phone OTP.
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN", default="")
WHATSAPP_TEMPLATE_NAME = env("WHATSAPP_TEMPLATE_NAME", default="")
WHATSAPP_TEMPLATE_LANGUAGE = env("WHATSAPP_TEMPLATE_LANGUAGE", default="en_US")
