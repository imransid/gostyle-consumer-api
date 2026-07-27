import uuid

from django.db import migrations, models


def relabel_existing_rows(apps, schema_editor):
    """Carry pre-refactor OTP rows onto the new destination_type / purpose
    vocabulary. Those rows only ever backed the phone sign-up flow, so anything
    that is not already an email maps to phone, and every purpose maps to
    register.
    """
    OtpCode = apps.get_model("accounts", "OtpCode")
    OtpCode.objects.exclude(destination_type="email").update(destination_type="phone")
    OtpCode.objects.update(purpose="register")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_phone_optional"),
    ]

    operations = [
        # --- OtpCode: channel -> destination_type, new destination_type/purpose vocab ---
        migrations.RemoveIndex(
            model_name="otpcode",
            name="otp_code_dest_purpose_idx",
        ),
        migrations.RenameField(
            model_name="otpcode",
            old_name="channel",
            new_name="destination_type",
        ),
        migrations.AlterField(
            model_name="otpcode",
            name="destination_type",
            field=models.CharField(
                choices=[("phone", "phone"), ("email", "email")], max_length=10
            ),
        ),
        migrations.AlterField(
            model_name="otpcode",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("register", "register"),
                    ("login", "login"),
                    ("password_reset", "password_reset"),
                ],
                max_length=20,
            ),
        ),
        migrations.RunPython(relabel_existing_rows, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="otpcode",
            index=models.Index(
                condition=models.Q(consumed_at__isnull=True),
                fields=["destination", "purpose", "-created_at"],
                name="otp_code_live_idx",
            ),
        ),
        # --- Verification ------------------------------------------------------
        migrations.CreateModel(
            name="Verification",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("destination", models.CharField(db_index=True, max_length=254)),
                (
                    "destination_type",
                    models.CharField(
                        choices=[("phone", "phone"), ("email", "email")], max_length=10
                    ),
                ),
                (
                    "purpose",
                    models.CharField(
                        choices=[
                            ("register", "register"),
                            ("login", "login"),
                            ("password_reset", "password_reset"),
                        ],
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "verification",
            },
        ),
        migrations.AddIndex(
            model_name="verification",
            index=models.Index(
                condition=models.Q(consumed_at__isnull=True),
                fields=["destination", "destination_type", "purpose", "-created_at"],
                name="verification_live_idx",
            ),
        ),
    ]
