"""GeoIP city lookup from an IP address.

Uses MaxMind's GeoLite2-City (free) or GeoIP2-City database, read locally
with the `geoip2` library.  A lookup takes microseconds, no request leaves
the server.

Settings
--------
GEOIP_DATABASE  path to the .mmdb file  (default: <BASE_DIR>/geoip/GeoLite2-City.mmdb)

The database should be refreshed monthly; MaxMind publishes updates weekly.
"""

import ipaddress
import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_reader = None  # Lazy-loaded singleton


def _get_reader():
    """Return a geoip2 DatabaseReader, or None if the database is missing."""
    global _reader
    if _reader is not None:
        return _reader

    try:
        import geoip2.database  # noqa: delay import so the dep is soft
    except ImportError:
        logger.warning("geoip2 library not installed — location lookup disabled")
        return None

    db_path = getattr(
        settings,
        "GEOIP_DATABASE",
        Path(settings.BASE_DIR) / "geoip" / "GeoLite2-City.mmdb",
    )
    db_path = Path(db_path)

    if not db_path.exists():
        logger.warning("GeoIP database not found at %s — location lookup disabled", db_path)
        return None

    try:
        _reader = geoip2.database.Reader(str(db_path))
        logger.info("GeoIP database loaded from %s", db_path)
    except Exception:
        logger.exception("Failed to open GeoIP database at %s", db_path)
        return None

    return _reader


def is_private_ip(ip_str: str) -> bool:
    """True for private, reserved, loopback, and link-local addresses."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Malformed → treat as private, skip lookup
    return addr.is_private or addr.is_reserved or addr.is_loopback or addr.is_link_local


COUNTRY_CODE_DISPLAY = {
    "US": "USA",
    "GB": "UK",
}


def location_details_for_ip(ip: str | None) -> tuple[str | None, str | None]:
    """Return (location, full_location) for *ip*.

    location: City and country code, e.g. "Dhaka, BD", "New York, USA", or country name if no city.
    full_location: Full location name including city, region, and country.
                   E.g. "Dubai, United Arab Emirates" or "Austin, Texas, United States".

    Returns (None, None) when:
    - ip is None, empty or private/reserved
    - the database is missing or the library is not installed
    - the address is not in the database
    """
    if not ip:
        return (None, None)

    ip = ip.strip()
    if is_private_ip(ip):
        return (None, None)

    reader = _get_reader()
    if reader is None:
        return (None, None)

    try:
        resp = reader.city(ip)
    except Exception:
        # geoip2.errors.AddressNotFoundError or anything else
        return (None, None)

    city = None
    if getattr(resp, "city", None) and resp.city.names:
        c = resp.city.names.get("en")
        if isinstance(c, str) and c:
            city = c

    country = None
    if getattr(resp, "country", None) and resp.country.names:
        c = resp.country.names.get("en")
        if isinstance(c, str) and c:
            country = c

    country_code = None
    if getattr(resp, "country", None) and getattr(resp.country, "iso_code", None):
        code = resp.country.iso_code
        if isinstance(code, str) and code:
            code = code.strip().upper()
            country_code = COUNTRY_CODE_DISPLAY.get(code, code)

    subdivision = None
    if getattr(resp, "subdivisions", None):
        try:
            s = resp.subdivisions.most_specific.names.get("en")
            if isinstance(s, str) and s:
                subdivision = s
        except Exception:
            pass

    # location format: e.g. "Dhaka, BD", "New York, USA"
    if city and country_code:
        location = f"{city}, {country_code}"
    elif city:
        location = city
    elif country:
        location = country
    else:
        location = None

    parts = []
    if city:
        parts.append(city)
    if subdivision and subdivision != city:
        parts.append(subdivision)
    if country:
        parts.append(country)

    full_location = ", ".join(parts) if parts else None
    return (location, full_location)


def city_for_ip(ip: str | None) -> str | None:
    """Return the English city name for *ip*, or the country if no city.

    Returns None when:
    - ip is None, empty or private/reserved
    - the database is missing or the library is not installed
    - the address is not in the database
    """
    return location_details_for_ip(ip)[0]

