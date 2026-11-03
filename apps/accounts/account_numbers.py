"""
The Account ID on the Settings screen: "GS-8841-2290".

For a person to read out to support, so it is short and has no letters to
mix up. RANDOM, not made from the id or from the row count: a number that
counts up tells anyone holding two of them how many accounts exist, and 8
digits cut from a uuid would repeat between two accounts long before 10^8.

Unique by the column's index. new() checks first, so a repeat costs one more
draw, and the index is the backstop for two sign-ups drawing the same number
in the same instant.

A row written by code from before the column has none until ensure() fills
it, on its first /auth/me read. The migration fills every row that existed.
"""

import secrets

from django.db import IntegrityError, transaction

PREFIX = "GS"
# Ten repeats in a row only happens with the space nearly full.
TRIES = 10


def make():
    """A random number in the format, taken or not."""
    n = secrets.randbelow(10**8)
    return f"{PREFIX}-{n // 10**4:04d}-{n % 10**4:04d}"


def new():
    """A number no account holds yet."""
    from .models import ConsumerAccount

    for _ in range(TRIES):
        candidate = make()
        # all_objects: a purged account keeps its number.
        if not ConsumerAccount.all_objects.filter(account_number=candidate).exists():
            return candidate
    raise RuntimeError("No free account number after %d tries." % TRIES)


def ensure(account):
    """The account's number, given one first if it has none.

    Writes only when the column is null, so two reads at once cannot give
    one account two numbers: the second finds it set and reads the first's.
    """
    if account.account_number:
        return account.account_number

    from .models import ConsumerAccount

    rows = ConsumerAccount.all_objects.filter(pk=account.pk, account_number__isnull=True)
    for _ in range(TRIES):
        try:
            # A savepoint, so a repeat does not break the request's transaction.
            with transaction.atomic():
                rows.update(account_number=new())
            break
        except IntegrityError:
            continue
    account.account_number = (
        ConsumerAccount.all_objects.filter(pk=account.pk)
        .values_list("account_number", flat=True)
        .first()
    )
    return account.account_number
