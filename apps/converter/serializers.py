from rest_framework import serializers
from .models import ConversionJob


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