"""
gunicorn's access log, with the query string left out where it carries
personal data.

`GET /api/v1/user/lookup?contact=` puts a member's email or phone in the URL,
and gunicorn's access line (docker-compose.yml, --access-logformat) records
`query`. nginx drops the same field for the same path in its own log
(nginx/log-format-json.conf); this is gunicorn's half.

The phone's position on GET /discover/map (`user_latitude`,
`user_longitude`, MAP_COUNTRY_V1) is only for picking the customer's country
and must not be logged: those two params are cut out of the query on every
path, and the rest of it is kept. nginx does the same
(nginx/log-format-json.conf).

Every atom that carries the query is changed, not only the `%(q)s` the
format uses today: `%(r)s` (the request line) and the raw environ atoms
`%({query_string}e)s` and `%({raw_uri}e)s` all hold it too, and a format
change should not quietly bring the contact back.

Loaded by gunicorn itself, before Django: `--logger-class
config.gunicorn_logging.AccessLogger`. Nothing here may import Django.
"""

import re
from urllib.parse import unquote_plus

from gunicorn.glogging import Logger

# The paths whose query is never logged. The match ignores case, a trailing
# slash and doubled slashes: nginx passes the path on as the client sent it,
# and a spelling Django answers with a 404 still reaches this log.
PRIVATE_QUERY_PATHS = re.compile(r"^/api/v1/user/lookup/?$", re.IGNORECASE)

_SLASHES = re.compile(r"/{2,}")

# The params never logged on any path, by their name as Django reads it.
PRIVATE_PARAMS = frozenset({"user_latitude", "user_longitude"})


def private_query(path):
    """True when the query string of a request to `path` must not be logged."""
    return bool(PRIVATE_QUERY_PATHS.match(_SLASHES.sub("/", path or "")))


def without_private_params(query):
    """
    The query string without PRIVATE_PARAMS, every other param as it came.
    Split on "&" only, as Django splits it, and each name decoded, so
    `user%5Flatitude` is cut out too.
    """
    if not query:
        return query
    kept = [
        part for part in query.split("&")
        if unquote_plus(part.split("=", 1)[0]) not in PRIVATE_PARAMS
    ]
    return "&".join(kept)


class AccessLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        path = environ.get("PATH_INFO") or ""
        query = environ.get("QUERY_STRING") or ""
        if private_query(path):
            query = ""
        else:
            kept = without_private_params(query)
            if kept == query:
                return atoms
            query = kept
        target = f"{path}?{query}" if query else path
        atoms["q"] = query
        atoms["r"] = "%s %s %s" % (
            environ.get("REQUEST_METHOD"), target, environ.get("SERVER_PROTOCOL")
        )
        atoms["{query_string}e"] = query
        atoms["{raw_uri}e"] = target
        return atoms
