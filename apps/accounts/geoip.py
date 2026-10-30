"""
The place an IP address is in, for a session's `location`: the city and a
short country code ("Dubai, UAE", "Dhaka, BD").

Read from a local MaxMind database (GeoLite2 City or GeoIP2 City) with
MaxMind's own reader, so no request leaves the server and a lookup takes
microseconds. The file is GEOIP_CITY_DB; with none, every lookup is KEEP and
locations stay as they are (null for a new session). docs/DEVICE_SESSIONS_SERVER.md
says how to put the file on the server and refresh it.

City level only, on purpose: the list needs a place name, and that is all an
IP address can honestly tell. The coordinates the file also holds are never
read. Expect it to be wrong sometimes: carriers route through a few gateways,
and a VPN shows the VPN's city.
"""

import ipaddress
import logging
import os
import threading

import maxminddb
from django.conf import settings

logger = logging.getLogger(__name__)

# "Nothing to look up": the caller leaves the stored location as it was.
KEEP = object()

# The country after the city is its ISO 3166 two-letter code, except these,
# which people know by another short name (Rafa, 2026-10-30).
_SHORT_NAMES = {"US": "USA", "GB": "UK", "AE": "UAE"}

_lock = threading.Lock()
_reader = None
_opened = None  # (path, mtime) of the file _reader has open


def location(ip):
    """Where a public IP is: "Dhaka, BD" (the English city name and the
    country's short code), else the city alone, else the country's English
    name ("United Arab Emirates"), else None.

    KEEP for a private or reserved address (local testing, it gives no place),
    and whenever the database is not there or cannot be read.
    """
    address = public_address(ip)
    if address is None:
        return KEEP

    reader = _current_reader()
    if reader is None:
        return KEEP
    try:
        record = reader.get(str(address))
    except (ValueError, maxminddb.InvalidDatabaseError) as e:
        logger.warning("geoip lookup failed for an address: %s", e)
        return KEEP

    return _describe(record)


def public_address(ip):
    """`ip` as an address when it is a public one, the only kind with a place
    (an IPv4 inside IPv6 counts as the IPv4), else None: private, reserved,
    or not an address at all."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address if address.is_global else None


def database():
    """The open database's metadata (database_type, build_epoch), or None
    when there is none to read."""
    reader = _current_reader()
    return reader.metadata() if reader is not None else None


def _describe(record):
    """location()'s words for one database record (a dict, or None)."""
    record = record or {}
    city = record.get("city", {}).get("names", {}).get("en")
    # A few networks have only the country they are registered in.
    country = record.get("country") or record.get("registered_country") or {}
    code = country.get("iso_code")
    if city:
        return f"{city}, {_SHORT_NAMES.get(code, code)}" if code else city
    return country.get("names", {}).get("en")


def _current_reader():
    """The open database, reopened when the file has been replaced.

    The monthly refresh swaps the file in place; a stat per lookup notices it,
    and lookups only happen when a session's IP changes.
    """
    global _reader, _opened

    path = settings.GEOIP_CITY_DB
    if not path:
        return None
    # While a new file is being swapped in, the one already open still answers.
    still_open = _reader if _opened and _opened[0] == path else None
    try:
        stamp = (path, os.stat(path).st_mtime)
    except OSError as e:
        logger.warning("cannot read GEOIP_CITY_DB %s: %s", path, e)
        return still_open

    if stamp == _opened:
        return _reader
    with _lock:
        if stamp != _opened:
            try:
                reader = maxminddb.open_database(path)
            except (OSError, maxminddb.InvalidDatabaseError) as e:
                logger.warning("cannot open GEOIP_CITY_DB %s: %s", path, e)
                return still_open
            # The old reader is not closed: a lookup may still be using it.
            # It goes when nothing refers to it.
            _reader, _opened = reader, stamp
    return _reader
