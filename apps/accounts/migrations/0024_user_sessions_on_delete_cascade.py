from django.db import migrations


class Migration(migrations.Migration):
    """Its own migration: Django adds 0023's foreign keys only once 0023's
    other operations are done, so there it would find none to swap."""

    dependencies = [
        ("accounts", "0023_user_sessions"),
    ]

    operations = [
        # user_id REFERENCES consumer_account(id) ON DELETE CASCADE, as the
        # brief's table has it. Django's own foreign key has no ON DELETE
        # and is DEFERRABLE; it is swapped for the brief's. Django finds a
        # foreign key by looking it up, never by name, so later migrations
        # are not affected.
        migrations.RunSQL(
            sql="""
                DO $$
                DECLARE fk text;
                BEGIN
                    SELECT conname INTO fk FROM pg_constraint
                    WHERE conrelid = 'user_sessions'::regclass AND contype = 'f'
                      AND conkey = ARRAY[(
                          SELECT attnum FROM pg_attribute
                          WHERE attrelid = 'user_sessions'::regclass AND attname = 'user_id'
                      )];
                    EXECUTE format('ALTER TABLE user_sessions DROP CONSTRAINT %I', fk);
                END $$;
                ALTER TABLE user_sessions ADD CONSTRAINT user_sessions_user_id_fkey
                    FOREIGN KEY (user_id) REFERENCES consumer_account (id) ON DELETE CASCADE;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
