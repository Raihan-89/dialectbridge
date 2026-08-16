from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("converter", "0005_migrationjob_progress")]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="pending_deletion_token",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="migrationjob",
            name="pending_deletion_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
