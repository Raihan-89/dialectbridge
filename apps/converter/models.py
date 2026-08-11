from django.conf import settings
from django.db import models


class ConversionJob(models.Model):
    """
    Records a single SQL conversion request: the source SQL, the direction
    of conversion, the resulting converted SQL, and any warnings raised
    (e.g. types flagged for manual review). This is the audit trail /
    history feature.
    """

    class Direction(models.TextChoices):
        MSSQL_TO_POSTGRES = "mssql_to_postgres", "SQL Server → PostgreSQL"
        POSTGRES_TO_MSSQL = "postgres_to_mssql", "PostgreSQL → SQL Server"

    class StatementType(models.TextChoices):
        DDL = "ddl", "DDL (CREATE TABLE, etc.)"
        DML = "dml", "DML (SELECT/INSERT/UPDATE/DELETE)"
        PROCEDURE = "procedure", "Stored Procedure / Function"
        TRIGGER = "trigger", "Trigger"

    direction = models.CharField(max_length=32, choices=Direction.choices)
    statement_type = models.CharField(
        max_length=16, choices=StatementType.choices, default=StatementType.DDL
    )

    source_sql = models.TextField()
    converted_sql = models.TextField(blank=True)

    # Stored as JSON list of warning strings, e.g.
    # ["MANUAL REVIEW REQUIRED: 'GEOGRAPHY' has no clean equivalent"]
    warnings = models.JSONField(default=list, blank=True)

    # Whether conversion completed without raising an exception
    # (separate from warnings, which can exist even on success)
    succeeded = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversion_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_direction_display()} ({self.statement_type}) — {self.created_at:%Y-%m-%d %H:%M}"