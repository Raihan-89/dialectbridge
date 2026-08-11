from rest_framework import serializers
from .models import ConversionJob, DatabaseConnection, MigrationJob


class ConversionRequestSerializer(serializers.Serializer):
    """
    Input serializer for a new conversion request.
    Not tied to the model directly, since the request only needs source_sql
    + direction — converted_sql/warnings are OUTPUT, produced by the engine.
    """
    source_sql = serializers.CharField(
        help_text="The raw SQL statement(s) to convert."
    )
    direction = serializers.ChoiceField(choices=ConversionJob.Direction.choices)
    statement_type = serializers.ChoiceField(
        choices=ConversionJob.StatementType.choices,
        default=ConversionJob.StatementType.DDL,
    )


class ConversionJobSerializer(serializers.ModelSerializer):
    """Output serializer — represents a saved conversion job, for history/detail views."""

    class Meta:
        model = ConversionJob
        fields = [
            "id",
            "direction",
            "statement_type",
            "source_sql",
            "converted_sql",
            "warnings",
            "succeeded",
            "error_message",
            "created_at",
        ]
        read_only_fields = fields


class DatabaseConnectionSerializer(serializers.ModelSerializer):
    """CRUD serializer for a saved database connection.

    The password is write-only: it is obfuscated on the model and never
    returned by the API.
    """

    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    effective_port = serializers.IntegerField(read_only=True)

    class Meta:
        model = DatabaseConnection
        fields = [
            "id", "name", "engine", "role", "host", "port", "database",
            "username", "password", "effective_port", "created_at",
        ]
        read_only_fields = ["created_at"]

    def create(self, validated_data):
        password = validated_data.pop("password", "")
        instance = super().create(validated_data)
        if password:
            instance.set_password(password)
            instance.save(update_fields=["password"])
        return instance

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        instance = super().update(instance, validated_data)
        if password:
            instance.set_password(password)
            instance.save(update_fields=["password"])
        return instance


class MigrationJobSerializer(serializers.ModelSerializer):
    """Represents a migration job; the full report is stored as JSON."""

    source = serializers.PrimaryKeyRelatedField(queryset=DatabaseConnection.objects.all())
    target = serializers.PrimaryKeyRelatedField(queryset=DatabaseConnection.objects.all())
    source_name = serializers.CharField(source="source.name", read_only=True)
    target_name = serializers.CharField(source="target.name", read_only=True)

    class Meta:
        model = MigrationJob
        fields = [
            "id", "name", "source", "target", "source_name", "target_name",
            "copy_data", "reset_target", "status", "report", "warnings", "error_message",
            "started_at", "finished_at", "created_at",
        ]
        read_only_fields = [
            "status", "report", "warnings", "error_message",
            "started_at", "finished_at",
        ]
