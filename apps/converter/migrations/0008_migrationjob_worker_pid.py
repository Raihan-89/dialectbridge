from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("converter", "0007_migrationjob_cancel_requested"),
    ]

    operations = [
        migrations.AddField(
            model_name="migrationjob",
            name="worker_pid",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="OS pid of the thread running this job.",
                null=True,
            ),
        ),
    ]