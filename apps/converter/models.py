from django.conf import settings
from django.core import signing
from django.db import models


class DatabaseConnection(models.Model):
    """
    A saved live database connection (source or target) used by the
    migration engine. Passwords are stored obfuscated with Django's signing
    machinery (keyed on SECRET_KEY) rather than in plain text.
    """

    class Engine(models.TextChoices):
        MSSQL = "mssql", "SQL Server (MSSQL)"
        POSTGRES = "postgres", "PostgreSQL"

    class Role(models.TextChoices):
        SOURCE = "source", "Source"
        TARGET = "target", "Target"

    name = models.CharField(max_length=100)
    engine = models.CharField(max_length=16, choices=Engine.choices)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.SOURCE)
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=0, help_text="Leave 0 to use the driver default.")
    database = models.CharField(max_length=255)
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=512, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="database_connections",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["name", "created_by"], name="uniq_connection_name_per_user"),
        ]

    def __str__(self):
        return f"{self.name} ({self.engine} → {self.host}:{self.port}/{self.database})"

    def effective_port(self) -> int:
        return self.port or (1433 if self.engine == self.Engine.MSSQL else 5432)

    def get_password(self) -> str:
        if not self.password:
            return ""
        try:
            return signing.loads(self.password)
        except signing.BadSignature:
            return ""

    def set_password(self, raw: str) -> None:
        self.password = signing.dumps(raw)


class MigrationJob(models.Model):
    """
    A single end-to-end database migration run between two saved connections.
    Holds the full per-object report so results can be inspected later.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        PARTIAL = "partial", "Completed with errors"

    name = models.CharField(max_length=200, blank=True)
    source = models.ForeignKey(
        DatabaseConnection, on_delete=models.CASCADE, related_name="source_migrations"
    )
    target = models.ForeignKey(
        DatabaseConnection, on_delete=models.CASCADE, related_name="target_migrations"
    )
    copy_data = models.BooleanField(default=True, help_text="Copy table data, not just schema.")
    reset_target = models.BooleanField(
        default=False,
        help_text="Drop target schemas being migrated into before running (destructive to prior runs).",
    )

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    cancel_requested = models.BooleanField(default=False, help_text="User asked to stop this migration.")
    worker_pid = models.PositiveIntegerField(null=True, blank=True, help_text="OS pid of the thread running this job.")
    report = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    progress_stage = models.CharField(max_length=255, blank=True, default="Queued")
    pending_deletion_token = models.UUIDField(null=True, blank=True, db_index=True)
    pending_deletion_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="migration_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name or 'Migration'} {self.source} → {self.target} [{self.status}]"


class MigrationError(models.Model):
    """
    A single failure captured from a migration run, stored separately from
    the report JSON so errors can be listed, filtered and analyzed as a table
    (object kind, error message, failing SQL, source line, ...).
    """

    class ObjectKind(models.TextChoices):
        TABLE = "table", "Table"
        INDEX = "index", "Index"
        CONSTRAINT = "constraint", "Constraint"
        VIEW = "view", "View"
        FUNCTION = "function", "Function"
        PROCEDURE = "procedure", "Procedure"
        TRIGGER = "trigger", "Trigger"
        DATA = "data", "Data copy"
        OTHER = "object", "Object"

    job = models.ForeignKey(
        MigrationJob, on_delete=models.CASCADE, related_name="errors"
    )
    object_kind = models.CharField(max_length=16, choices=ObjectKind.choices, default=ObjectKind.OTHER)
    object_name = models.CharField(max_length=255, blank=True)
    error_type = models.CharField(max_length=64, blank=True, default="sql_error")
    message = models.TextField(blank=True)
    detail = models.TextField(blank=True, help_text="Failing statement / extra context.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_object_kind_display()} {self.object_name}: {self.message[:60]}"


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
