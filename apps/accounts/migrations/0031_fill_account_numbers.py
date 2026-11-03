"""
Give every account that exists an account number (accounts/account_numbers.py).

Purged accounts too: a support case can name one. The format is copied here,
not imported, so a later change to the module cannot change what this did.
Reversing leaves the numbers: 0030's reverse drops the column anyway.
"""

import secrets

from django.db import migrations


def _make():
    n = secrets.randbelow(10**8)
    return f"GS-{n // 10**4:04d}-{n % 10**4:04d}"


def fill(apps, schema_editor):
    ConsumerAccount = apps.get_model("accounts", "ConsumerAccount")
    # _base_manager: every row, whatever managers the model state carries.
    rows = ConsumerAccount._base_manager
    taken = set(
        rows.filter(account_number__isnull=False).values_list("account_number", flat=True)
    )
    for pk in rows.filter(account_number__isnull=True).values_list("pk", flat=True):
        number = _make()
        while number in taken:
            number = _make()
        taken.add(number)
        rows.filter(pk=pk).update(account_number=number)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0030_display_name_account_number"),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
