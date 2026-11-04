"""
Verified salons (MAP_FILTER_VERIFIED_V1, the audit's D2): staff mark a salon
verified in the admin (models.SalonDiscoveryFlag), the app filters on it and
shows a badge.

ONE RULE, IN SQL. `is_verified` is an EXISTS on our flag table, added to the
same query that reads the salons (selectors.discoverable_salons, the map's
query, the suggestions), so a list of any length costs no extra query. A
salon with no flag row, or a row set to no, is not verified.

Off: nothing here runs. No annotation, no `is_verified` key, `verified=true`
is ignored and `additional=verified` stays the 400 it was.
"""

from django.conf import settings
from django.db.models import Exists, OuterRef

from .models import SalonDiscoveryFlag
from .params import TRUE_VALUES

# The map drawer's Additional chip (and the id the app's map contract uses).
OPTION = ("verified", "Verified salons")


def is_on():
    return settings.MAP_FILTER_VERIFIED_V1


def expression():
    """EXISTS: a flag row says yes for this salon (the outer row's pk, its storefront id)."""
    return Exists(SalonDiscoveryFlag.objects.filter(salon_id=OuterRef("pk"), is_verified=True))


def annotate(qs):
    """`qs` (salons, storefront rows) with `is_verified`, while the switch is on; untouched off."""
    if not is_on():
        return qs
    return qs.annotate(is_verified=expression())


def wanted(params):
    """
    The `verified` param asks for verified salons only: true (1, yes, on),
    with the switch on. false, anything unknown, or nothing: no filter.
    """
    if not is_on():
        return False
    raw = params.get("verified")
    return str(raw or "").strip().lower() in TRUE_VALUES


def salon_names():
    """{salon id: name} of every public salon: the published name, else the branch's."""
    from .selectors import discoverable_salons, with_published_card_fields

    rows = with_published_card_fields(discoverable_salons()).values("id", "published_name", "branch_name")
    return {row["id"]: (row["published_name"] or row["branch_name"] or "")[:200] for row in rows}


def sync_flags(names=None):
    """
    A row for every public salon (not verified) and every name current: what
    the admin list shows. Returns (rows added, names changed). Rows of salons
    no longer public are kept, with their mark.
    """
    names = salon_names() if names is None else names
    existing = {flag.salon_id: flag for flag in SalonDiscoveryFlag.objects.filter(salon_id__in=list(names))}

    missing = [
        SalonDiscoveryFlag(salon_id=salon_id, salon_name=name)
        for salon_id, name in names.items()
        if salon_id not in existing
    ]
    SalonDiscoveryFlag.objects.bulk_create(missing, ignore_conflicts=True)

    renamed = [flag for salon_id, flag in existing.items() if flag.salon_name != names[salon_id]]
    for flag in renamed:
        flag.salon_name = names[flag.salon_id]
    if renamed:
        SalonDiscoveryFlag.objects.bulk_update(renamed, ["salon_name"])
    return len(missing), len(renamed)
