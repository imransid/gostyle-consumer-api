from .base import *  # noqa

DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# TLS terminates at host nginx (nginx/api.gostyle.uk.conf); every request then
# reaches gunicorn over plain HTTP on 3850. Without this, Django considers each
# one insecure -- is_secure() is False, so SECURE_SSL_REDIRECT would redirect a
# request that already arrived over HTTPS, forever. nginx overwrites
# X-Forwarded-Proto on every request, so a client cannot forge it.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = env.bool("DJANGO_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("DJANGO_COOKIE_SECURE", default=True)

# One year. Set DJANGO_HSTS_SECONDS=0 while first bringing a domain up: once a
# browser has seen this header it refuses plain HTTP for that long, whatever the
# server later says.
SECURE_HSTS_SECONDS = env.int("DJANGO_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "DJANGO_HSTS_INCLUDE_SUBDOMAINS", default=False
)
SECURE_HSTS_PRELOAD = env.bool("DJANGO_HSTS_PRELOAD", default=False)
SECURE_REFERRER_POLICY = "same-origin"

# Behind a proxy every request appears to come from it, which would make DRF's
# "10/min" login throttle a single global bucket. NUM_PROXIES=1 tells DRF to
# read the last entry of X-Forwarded-For instead -- the address nginx itself
# observed, appended by $proxy_add_x_forwarded_for.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "NUM_PROXIES": env.int("DJANGO_NUM_PROXIES", default=1),
}
