from django.db import migrations

# The brief's four 2-step tables (docs/TWO_FACTOR_API.md section 8).
TABLES = ("user_authenticators", "verification_codes", "verification_tokens", "login_challenges")


class Migration(migrations.Migration):
    """Its own migration, as 0024 is for user_sessions: Django adds 0025's
    foreign keys only once 0025's other operations are done, so there it
    would find none to swap."""

    dependencies = [
        ("accounts", "0025_two_factor"),
    ]

    operations = [
        # user_id REFERENCES consumer_account(id) ON DELETE CASCADE, as the
        # brief's tables have it, in place of Django's own foreign key (no ON
        # DELETE, DEFERRABLE). Django finds a foreign key by looking it up,
        # never by name, so later migrations are not affected.
        migrations.RunSQL(
            sql="".join(
                f"""
                DO $$
                DECLARE fk text;
                BEGIN
                    SELECT conname INTO fk FROM pg_constraint
                    WHERE conrelid = '{table}'::regclass AND contype = 'f'
                      AND conkey = ARRAY[(
                          SELECT attnum FROM pg_attribute
                          WHERE attrelid = '{table}'::regclass AND attname = 'user_id'
                      )];
                    EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', fk);
                END $$;
                ALTER TABLE {table} ADD CONSTRAINT {table}_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES consumer_account (id) ON DELETE CASCADE;
                """
                for table in TABLES
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Django gives a text primary key a second index for LIKE queries.
        # A token hash is only ever looked up whole, by its primary key.
        migrations.RunSQL(
            sql="""
                DROP INDEX IF EXISTS login_challenges_token_hash_58efb480_like;
                DROP INDEX IF EXISTS verification_tokens_token_hash_c6263a0b_like;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
