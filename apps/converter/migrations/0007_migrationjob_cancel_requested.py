from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("converter", "0006_migrationjob_pending_deletion"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="cancel_requested",
            field=models.BooleanField(
                default=False,
                help_text="User asked to stop this migration.",
            ),
        ),
    ]