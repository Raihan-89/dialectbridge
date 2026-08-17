from django.utils import timezone
import logging
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from engine.connectors.base import ConnectorError
from engine.service import convert_sql, UnsupportedStatementTypeError
from . import migration_service
from .models import ConversionJob, DatabaseConnection, MigrationJob
from .serializers import (
    ConversionRequestSerializer,
    ConversionJobSerializer,
    DatabaseConnectionSerializer,
    MigrationJobSerializer,
)

logger = logging.getLogger("dialectbridge.api")


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

        job = ConversionJob(
            direction=direction,
            statement_type=statement_type,
            source_sql=source_sql,
            created_by=request.user if request.user.is_authenticated else None,
        )

        unsupported = False
        try:
            result = convert_sql(source_sql, direction, statement_type)
            job.converted_sql = result.sql
            job.warnings = result.warnings
            job.succeeded = True
        except UnsupportedStatementTypeError as exc:
            unsupported = True
            job.succeeded = False
            job.error_message = str(exc)
            logger.warning("SQL conversion rejected direction=%s statement_type=%s error=%s", direction, statement_type, exc)
        except Exception as exc:
            job.succeeded = False
            job.error_message = str(exc)
            logger.exception("SQL conversion failed direction=%s statement_type=%s", direction, statement_type)
        finally:
            job.save()

        output_serializer = ConversionJobSerializer(job)
        if job.succeeded:
            response_status = status.HTTP_200_OK
        elif unsupported:
            response_status = status.HTTP_501_NOT_IMPLEMENTED
        else:
            response_status = status.HTTP_422_UNPROCESSABLE_ENTITY
        return Response(output_serializer.data, status=response_status)


class ConversionJobViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    GET /api/jobs/          -> list of past conversions (history)
    GET /api/jobs/{id}/     -> a single conversion job's full detail
    Read-only: jobs are only created via ConvertSQLView, never edited directly.
    """
    queryset = ConversionJob.objects.all()
    serializer_class = ConversionJobSerializer


class DatabaseConnectionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                                mixins.CreateModelMixin, mixins.UpdateModelMixin,
                                mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """
    CRUD for saved database connections.

    POST   /api/connections/                 create a connection
    POST   /api/connections/{id}/test/       verify it can be reached
    GET    /api/connections/                 list
    GET    /api/connections/{id}/            detail
    PATCH  /api/connections/{id}/            update
    DELETE /api/connections/{id}/            remove
    """
    queryset = DatabaseConnection.objects.all()
    serializer_class = DatabaseConnectionSerializer

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        connection = self.get_object()
        try:
            result = migration_service.test_connection(connection)
        except ConnectorError as exc:
            logger.warning("Database connection test failed connection_id=%s error=%s", connection.pk, exc)
            return Response({"ok": False, "error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)


class MigrationJobViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          mixins.CreateModelMixin, viewsets.GenericViewSet):
    """
    Create + inspect database migration jobs.

    POST  /api/migrations/        {source, target, copy_data, name} -> runs the migration
    GET   /api/migrations/        list past runs
    GET   /api/migrations/{id}/   a run's full report
    """
    queryset = MigrationJob.objects.all()
    serializer_class = MigrationJobSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        job = MigrationJob(
            name=data.get("name", ""),
            source=data["source"],
            target=data["target"],
            copy_data=data.get("copy_data", True),
            reset_target=data.get("reset_target", False),
            status=MigrationJob.Status.RUNNING,
            started_at=timezone.now(),
            created_by=request.user if request.user.is_authenticated else None,
        )
        job.save()

        try:
            report = migration_service.run_migration(
                job.source, job.target,
                copy_data=job.copy_data, reset_target=job.reset_target,
            )
            job.report = report
            job.warnings = report.get("warnings", [])
            job.status = (
                MigrationJob.Status.COMPLETED if report.get("success")
                else MigrationJob.Status.PARTIAL
            )
        except Exception as exc:
            logger.exception("API migration job failed job_id=%s", job.pk)
            job.status = MigrationJob.Status.FAILED
            job.error_message = str(exc)
        finally:
            job.finished_at = timezone.now()
            job.save()

        out = self.get_serializer(job).data
        response_status = status.HTTP_201_CREATED if job.status in (
            MigrationJob.Status.COMPLETED, MigrationJob.Status.PARTIAL
        ) else status.HTTP_422_UNPROCESSABLE_ENTITY
        return Response(out, status=response_status)
