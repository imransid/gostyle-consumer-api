from django.db import migrations, models


def blank_emails_to_null(apps, schema_editor):
    ConsumerAccount = apps.get_model("accounts", "ConsumerAccount")
    ConsumerAccount.objects.filter(email="").update(email=None)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_otpcode"),
    ]

    operations = [
        # --- ConsumerAccount: email becomes a unique login identifier ---------
        # 1) allow NULL, 2) convert existing "" to NULL, 3) add the unique
        # constraint (multiple NULLs are allowed, so email-less accounts are OK).
        migrations.AlterField(
            model_name="consumeraccount",
            name="email",
            field=models.EmailField(blank=True, max_length=254, null=True),
        ),
        migrations.RunPython(blank_emails_to_null, noop),
        migrations.AlterField(
            model_name="consumeraccount",
            name="email",
            field=models.EmailField(blank=True, max_length=254, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="consumeraccount",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="consumeraccount",
            name="accepted_terms_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # --- OtpCode: generalise phone -> destination + channel + purpose -----
        migrations.RemoveIndex(
            model_name="otpcode",
            name="otp_code_phone_890623_idx",
        ),
        migrations.RenameField(
            model_name="otpcode",
            old_name="phone",
            new_name="destination",
        ),
        migrations.AlterField(
            model_name="otpcode",
            name="destination",
            field=models.CharField(db_index=True, max_length=254),
        ),
        migrations.AddField(
            model_name="otpcode",
            name="channel",
            field=models.CharField(
                choices=[("sms", "SMS"), ("whatsapp", "WhatsApp"), ("email", "Email")],
                default="sms",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="otpcode",
            name="purpose",
            field=models.CharField(
                choices=[("verify", "Verify contact"), ("reset", "Password reset")],
                default="verify",
                max_length=12,
            ),
        ),
        migrations.AddIndex(
            model_name="otpcode",
            index=models.Index(
                fields=["destination", "purpose", "consumed_at"],
                name="otp_code_dest_purpose_idx",
            ),
        ),
    ]
