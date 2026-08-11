from rest_framework import status, viewsets, mixins
from rest_framework.response import Response
from rest_framework.views import APIView

from engine.translators.ddl_translator import convert_ddl
from .models import ConversionJob
from .serializers import ConversionRequestSerializer, ConversionJobSerializer

# Maps the model's Direction choice to the (read, write) dialect args
# convert_ddl() expects. Keeping this mapping here, not in the model,
# since it's a detail of how the engine's API is called, not a fact
# about the data itself.
DIRECTION_TO_DIALECTS = {
    ConversionJob.Direction.MSSQL_TO_POSTGRES: ("tsql", "postgres"),
    ConversionJob.Direction.POSTGRES_TO_MSSQL: ("postgres", "tsql"),
}


class ConvertSQLView(APIView):
    """
    POST /api/convert/
    Body: {"source_sql": "...", "direction": "mssql_to_postgres", "statement_type": "ddl"}

    Runs the conversion engine, saves a ConversionJob record (history/audit
    trail), and returns the converted SQL + any warnings.
    """

    def post(self, request):
        input_serializer = ConversionRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        source_sql = data["source_sql"]
        direction = data["direction"]
        statement_type = data["statement_type"]
        read_dialect, write_dialect = DIRECTION_TO_DIALECTS[direction]

        job = ConversionJob(
            direction=direction,
            statement_type=statement_type,
            source_sql=source_sql,
            created_by=request.user if request.user.is_authenticated else None,
        )

        try:
            # Only DDL is wired up to the real engine so far — DML/procedures/
            # triggers will plug into the same pattern once those translators
            # exist.
            if statement_type == ConversionJob.StatementType.DDL:
                result = convert_ddl(source_sql, source_dialect=read_dialect, target_dialect=write_dialect)
                job.converted_sql = result.sql
                job.warnings = result.warnings
                job.succeeded = True
            else:
                raise NotImplementedError(
                    f"Conversion for statement_type='{statement_type}' isn't implemented yet."
                )
        except Exception as exc:
            job.succeeded = False
            job.error_message = str(exc)
        finally:
            job.save()

        output_serializer = ConversionJobSerializer(job)
        response_status = status.HTTP_200_OK if job.succeeded else status.HTTP_422_UNPROCESSABLE_ENTITY
        return Response(output_serializer.data, status=response_status)


class ConversionJobViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    GET /api/jobs/          -> list of past conversions (history)
    GET /api/jobs/{id}/     -> a single conversion job's full detail
    Read-only: jobs are only created via ConvertSQLView, never edited directly.
    """
    queryset = ConversionJob.objects.all()
    serializer_class = ConversionJobSerializer