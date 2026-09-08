"""Create the `verification` table that migration 0005 never created here.

0005_verify_then_register was edited in place twice on 2026-07-27, after
production had already applied it (merge bcdb477 at 07:21 ran migrate).
Django records applied migrations by name, so the rewritten 0005 — the one
that creates `verification` — has never run against production. Production
got the earlier version, which created `verification_token` instead.

Django's migration STATE already believes `verification` exists, so this uses
SeparateDatabaseAndState with no state_operations: database-only DDL, no state
change. Everything is IF (NOT) EXISTS so it is a no-op on databases built
after the rewrite (CI, newer dev machines), which already have the table.

`verification_token` is dropped rather than renamed: it has a NOT NULL UNIQUE
token_hash column that no current model supplies, so inserts would fail. Rows
there have a 10-minute TTL and the flow has been broken since July, so there
is nothing to preserve.

DO NOT EDIT A MIGRATION THAT HAS BEEN DEPLOYED. Add a new one instead.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_consumeraccount_gender"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        """
                        CREATE TABLE IF NOT EXISTS verification (
                            id uuid NOT NULL PRIMARY KEY,
                            destination varchar(254) NOT NULL,
                            destination_type varchar(10) NOT NULL,
                            purpose varchar(20) NOT NULL,
                            created_at timestamptz NOT NULL,
                            expires_at timestamptz NOT NULL,
                            consumed_at timestamptz NULL
                        );
                        """,
                        """
                        CREATE INDEX IF NOT EXISTS verification_destination_idx
                            ON verification (destination);
                        """,
                        """
                        CREATE INDEX IF NOT EXISTS verification_live_idx
                            ON verification (destination, destination_type, purpose, created_at DESC)
                            WHERE consumed_at IS NULL;
                        """,
                        """
                        DROP TABLE IF EXISTS verification_token;
                        """,
                    ],
                    reverse_sql=[
                        "DROP INDEX IF EXISTS verification_live_idx;",
                        "DROP INDEX IF EXISTS verification_destination_idx;",
                        "DROP TABLE IF EXISTS verification;",
                    ],
                ),
            ],
            state_operations=[],
        ),
    ]
    