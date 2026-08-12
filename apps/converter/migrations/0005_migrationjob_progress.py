from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("converter", "0004_migrationerror")]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="progress_percent",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="migrationjob",
            name="progress_stage",
            field=models.CharField(blank=True, default="Queued", max_length=255),
        ),
    ]
