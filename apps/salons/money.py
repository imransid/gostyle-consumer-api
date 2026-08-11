
"""
Minor units to a display number, in one place.

Every price in the platform is an integer in minor units: 19900 is 199.00.
Dividing by 100 in each serializer means one of them eventually forgets, or
uses float and produces 198.99999999999999.

Decimal, not float. Money in float is a bug waiting for a big enough number.
"""

from decimal import Decimal

MINOR_PER_MAJOR = Decimal(100)


def major(minor):
    """
    19900 -> Decimal('199.00'). None stays None.

    Returns Decimal so DRF renders it exactly. A caller that genuinely wants a
    JSON number can float() it, but that choice should be visible at the call
    site rather than hidden in here.
    """
    if minor is None:
        return None
    try:
        return Decimal(int(minor)) / MINOR_PER_MAJOR
    except (TypeError, ValueError):
        return None


def bps_to_percent(bps):
    """
    Basis points to a whole percent. 2500 -> 25.

    The storefront POLICY section takes a percent from the manager and the
    projection stores basis points, so reading it back needs the reverse.
    """
    if bps is None:
        return None
    try:
        return int(bps) // 100
    except (TypeError, ValueError):
        return None