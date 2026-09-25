"""
gunicorn's access log, with the query string left out where it carries
personal data.

`GET /api/v1/user/lookup?contact=` puts a member's email or phone in the URL,
and gunicorn's access line (docker-compose.yml, --access-logformat) records
`query`. nginx drops the same field for the same path in its own log
(nginx/log-format-json.conf); this is gunicorn's half.

Every atom that carries the query is blanked, not only the `%(q)s` the
format uses today: `%(r)s` (the request line) and the raw environ atoms
`%({query_string}e)s` and `%({raw_uri}e)s` all hold it too, and a format
change should not quietly bring the contact back.

Loaded by gunicorn itself, before Django: `--logger-class
config.gunicorn_logging.AccessLogger`. Nothing here may import Django.
"""

import re

from gunicorn.glogging import Logger

# The paths whose query is never logged. The match ignores case, a trailing
# slash and doubled slashes: nginx passes the path on as the client sent it,
# and a spelling Django answers with a 404 still reaches this log.
PRIVATE_QUERY_PATHS = re.compile(r"^/api/v1/user/lookup/?$", re.IGNORECASE)

_SLASHES = re.compile(r"/{2,}")


def private_query(path):
    """True when the query string of a request to `path` must not be logged."""
    return bool(PRIVATE_QUERY_PATHS.match(_SLASHES.sub("/", path or "")))


class AccessLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        path = environ.get("PATH_INFO") or ""
        if private_query(path):
            atoms["q"] = ""
            atoms["r"] = "%s %s %s" % (
                environ.get("REQUEST_METHOD"), path, environ.get("SERVER_PROTOCOL")
            )
            atoms["{query_string}e"] = ""
            atoms["{raw_uri}e"] = path
        return atoms
