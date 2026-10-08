from config.read_only_admin import register_read_only

# Platform tables live in the public schema, where the consumer_app DB user
# has SELECT-only access.
register_read_only("platform_data")
