"""
"Show my name to salons" (Privacy & Visibility).

One rule, used wherever a customer's name leaves for the salon side:

  - the salon's customer record (apps/salons/customer_sync.py),
  - booking-api's check-in desk and messages (gRPC GetConsumerContact),
  - a group booking's lanes (apps/salons/group_views._name_members).

Off, the salon sees the first name and the last name's initial, "Sara A.",
enough to call the right person at the desk and no more. A name of one
word stays as it is.
"""


def short_name(full_name):
    """'Sara Ahmed' -> 'Sara A.', 'Sara bin Ahmed' -> 'Sara A.', 'Sara' ->
    'Sara', blank -> ''."""
    parts = (full_name or "").split()
    if len(parts) <= 1:
        return " ".join(parts)
    return f"{parts[0]} {parts[-1][0].upper()}."


def name_for_salons(full_name, show):
    """The name the salon side may have: the whole one, or short_name."""
    return " ".join((full_name or "").split()) if show else short_name(full_name)
