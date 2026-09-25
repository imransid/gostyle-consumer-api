"""Set account_verified on members who proved a contact before the flag existed.

0006 added account_verified with default False, so a member who had already
verified a phone or email by then still reads as unverified. IsVerified and
the user lookup both trust the flag: left alone, those members would be
refused by one and invisible to the other.

A verification date is the proof. verify_otp_for_user writes the date and the
flag together, so a row with a date and no flag can only be one it missed.

Only rows still False are touched. The reverse is a no-op: once this has run a
back-filled row cannot be told apart from one verified since, and clearing the
flag on real, verified members would be worse than leaving it set.
"""

from django.db import migrations
from django.db.models import Q


def backfill_account_verified(apps, schema_editor):
    ConsumerAccount = apps.get_model("accounts", "ConsumerAccount")
    ConsumerAccount.objects.filter(
        Q(phone_verified_at__isnull=False) | Q(email_verified_at__isnull=False),
        account_verified=False,
    ).update(account_verified=True)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_favourite"),
    ]

    operations = [
        migrations.RunPython(backfill_account_verified, migrations.RunPython.noop),
    ]
