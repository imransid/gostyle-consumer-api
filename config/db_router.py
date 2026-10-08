"""Sends the admin's read-only apps to their own databases.

booking_data, review_data and push_data are other services' tables (see
DATABASES in config/settings/base.py). Every read of them goes to that
service's database, and nothing ever migrates there: those services own their
schemas.
"""

# app label -> DATABASES alias
READ_ONLY_DATABASES = {
    "booking_data": "booking",
    "review_data": "review",
    "push_data": "push",
}


class ReadOnlyDataRouter:
    def db_for_read(self, model, **hints):
        return READ_ONLY_DATABASES.get(model._meta.app_label)

    # The admin never writes these (config/read_only_admin.py) and the session
    # is read-only, but a write must still never land in our own database.
    def db_for_write(self, model, **hints):
        return READ_ONLY_DATABASES.get(model._meta.app_label)

    def allow_relation(self, obj1, obj2, **hints):
        db1 = READ_ONLY_DATABASES.get(obj1._meta.app_label)
        db2 = READ_ONLY_DATABASES.get(obj2._meta.app_label)
        if db1 or db2:
            return db1 == db2
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db in READ_ONLY_DATABASES.values():
            return False
        if app_label in READ_ONLY_DATABASES:
            return False
        return None
