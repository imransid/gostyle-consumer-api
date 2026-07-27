import uuid

from django.db import migrations, models


def copy_destination_to_identifier(apps, schema_editor):
    """Carry existing OTP rows over to the generalized identifier column.

    Pre-refactor rows only ever held phone destinations for the sign-up flow,
    so they map cleanly to channel="phone", purpose="register".
    """
    OtpCode = apps.get_model("accounts", "OtpCode")
    OtpCode.objects.update(
        identifier=models.F("destination"),
        channel="phone",
        purpose="register",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_phone_optional"),
    ]

    operations = [
        # --- OtpCode: destination -> identifier, new channel/purpose vocab -----
        migrations.RemoveIndex(
            model_name="otpcode",
            name="otp_code_dest_purpose_idx",
        ),
        migrations.AddField(
            model_name="otpcode",
            name="identifier",
            field=models.CharField(db_index=True, default="", max_length=254),
            preserve_default=False,
        ),
        migrations.RunPython(
            copy_destination_to_identifier, migrations.RunPython.noop
        ),
        migrations.RemoveField(
            model_name="otpcode",
            name="destination",
        ),
        migrations.AlterField(
            model_name="otpcode",
            name="channel",
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
        migrations.AddIndex(
            model_name="otpcode",
            index=models.Index(
                condition=models.Q(consumed_at__isnull=True),
                fields=["identifier", "purpose", "-created_at"],
                name="otp_code_live_idx",
            ),
        ),
        # --- VerificationToken -------------------------------------------------
        migrations.CreateModel(
            name="VerificationToken",
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
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("identifier", models.CharField(db_index=True, max_length=254)),
                (
                    "channel",
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
                "db_table": "verification_token",
            },
        ),
    ]
