from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_auth_foundation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="consumeraccount",
            name="phone",
            field=models.CharField(
                blank=True, db_index=True, max_length=20, null=True, unique=True
            ),
        ),
    ]
