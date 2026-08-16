import threading

from django.contrib import messages
from django.db import close_old_connections
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from engine.connectors.base import ConnectorError
from engine.service import convert_sql, UnsupportedStatementTypeError
from . import migration_service
from .verification_service import SECTION_LABELS, compare_live
from .models import ConversionJob, DatabaseConnection, MigrationError, MigrationJob

# Statement types the current engine can actually handle — others are shown
# disabled in the form until their translators land.
SUPPORTED_STATEMENT_TYPES = {"ddl", "dml"}


def convert_form_view(request):
    """
    Simple HTML form: paste SQL in, pick a direction + statement type, see
    converted SQL + warnings on the same page. Reuses the exact same engine
    call as the API view, and also saves a ConversionJob record — so
    conversions done via the web UI show up in history too.
    """
    context = {
        "direction_choices": ConversionJob.Direction.choices,
        "statement_type_choices": ConversionJob.StatementType.choices,
        "supported_statement_types": SUPPORTED_STATEMENT_TYPES,
    }

    if request.method == "POST":
        source_sql = request.POST.get("source_sql", "").strip()
        direction = request.POST.get("direction")
        statement_type = request.POST.get("statement_type", ConversionJob.StatementType.DDL)

        job = ConversionJob(
            direction=direction,
            statement_type=statement_type,
            source_sql=source_sql,
            created_by=request.user if request.user.is_authenticated else None,
        )

        try:
            result = convert_sql(source_sql, direction, statement_type)
            job.converted_sql = result.sql
            job.warnings = result.warnings
            job.succeeded = True
        except UnsupportedStatementTypeError as exc:
            job.error_message = str(exc)
        except Exception as exc:
            job.succeeded = False
            job.error_message = str(exc)
        finally:
            job.save()

        context.update({
            "source_sql": source_sql,
            "direction": direction,
            "statement_type": statement_type,
            "job": job,
        })

    return render(request, "converter/convert_form.html", context)


def history_view(request):
    """Lists past conversion jobs, most recent first."""
    jobs = ConversionJob.objects.all()[:50]
    return render(request, "converter/history.html", {"jobs": jobs})


def connections_view(request):
    """Manage saved database connections: list, create, test, delete."""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            conn = DatabaseConnection(
                name=request.POST.get("name", "").strip(),
                engine=request.POST.get("engine"),
                role=request.POST.get("role", DatabaseConnection.Role.SOURCE),
                host=request.POST.get("host", "").strip(),
                port=int(request.POST.get("port") or 0),
                database=request.POST.get("database", "").strip(),
                username=request.POST.get("username", "").strip(),
                created_by=request.user if request.user.is_authenticated else None,
            )
            conn.set_password(request.POST.get("password", ""))
            conn.save()
            messages.success(request, f"Connection '{conn.name}' saved.")
            return redirect("connections")
        if action == "delete":
            conn = get_object_or_404(DatabaseConnection, pk=request.POST.get("connection_id"))
            conn.delete()
            messages.success(request, "Connection deleted.")
            return redirect("connections")
        if action == "test":
            conn = get_object_or_404(DatabaseConnection, pk=request.POST.get("connection_id"))
            try:
                result = migration_service.test_connection(conn)
                messages.success(request, f"Connected: {result['server']}")
            except ConnectorError as exc:
                messages.error(request, f"Connection failed: {exc}")
            return redirect("connections")

    context = {
        "connections": DatabaseConnection.objects.all(),
        "engine_choices": DatabaseConnection.Engine.choices,
        "role_choices": DatabaseConnection.Role.choices,
    }
    return render(request, "converter/connections.html", context)


def migrate_view(request):
    """Run a migration between two saved connections and show the report."""
    if request.method == "POST":
        active = MigrationJob.objects.filter(status=MigrationJob.Status.RUNNING).first()
        if active:
            messages.warning(request, f"Migration #{active.pk} is already running.")
            return redirect("migrate-detail", pk=active.pk)
        source = get_object_or_404(DatabaseConnection, pk=request.POST.get("source"))
        target = get_object_or_404(DatabaseConnection, pk=request.POST.get("target"))
        copy_data = request.POST.get("copy_data") == "on"
        reset_target = request.POST.get("reset_target") == "on"

        job = MigrationJob(
            name=request.POST.get("name", "").strip() or f"{source.name} → {target.name}",
            source=source,
            target=target,
            copy_data=copy_data,
            reset_target=reset_target,
            status=MigrationJob.Status.RUNNING,
            started_at=timezone.now(),
            created_by=request.user if request.user.is_authenticated else None,
        )
        job.save()
        threading.Thread(target=_run_migration_job, args=(job.pk,), daemon=True).start()
        return redirect("migrate-detail", pk=job.pk)

    context = {
        "connections": DatabaseConnection.objects.all(),
        "recent_jobs": MigrationJob.objects.all(),
        "running_job": MigrationJob.objects.filter(status=MigrationJob.Status.RUNNING).first(),
    }
    return render(request, "converter/migrate.html", context)


def migrate_detail_view(request, pk):
    job = get_object_or_404(MigrationJob, pk=pk)
    return render(request, "converter/migrate_detail.html", {"job": job})


def migrate_status_view(request, pk):
    job = get_object_or_404(MigrationJob, pk=pk)
    return JsonResponse({
        "id": job.pk,
        "status": job.status,
        "status_label": job.get_status_display(),
        "progress_percent": job.progress_percent,
        "progress_stage": job.progress_stage,
        "finished": job.status in {
            MigrationJob.Status.COMPLETED, MigrationJob.Status.PARTIAL, MigrationJob.Status.FAILED,
        },
    })


def _run_migration_job(job_id: int) -> None:
    """Run a web-started migration outside the request and persist progress."""
    close_old_connections()
    try:
        job = MigrationJob.objects.select_related("source", "target").get(pk=job_id)

        def progress(percent, stage):
            MigrationJob.objects.filter(pk=job_id).update(
                progress_percent=percent, progress_stage=stage,
            )

        report = migration_service.run_migration(
            job.source, job.target, copy_data=job.copy_data,
            reset_target=job.reset_target, progress_callback=progress,
        )
        job.report = report
        job.warnings = report.get("warnings", [])
        job.status = MigrationJob.Status.COMPLETED if report.get("success") else MigrationJob.Status.PARTIAL
        job.progress_percent = 100
        job.progress_stage = "Migration completed" if report.get("success") else "Completed with errors"
        migration_service.record_migration_errors(job, report)
    except Exception as exc:
        job = MigrationJob.objects.get(pk=job_id)
        job.status = MigrationJob.Status.FAILED
        job.error_message = str(exc)
        job.progress_stage = "Migration failed"
    finally:
        job.finished_at = timezone.now()
        job.save()
        close_old_connections()


def errors_view(request):
    """Table of captured migration errors, filterable by kind / job / query."""
    kind = request.GET.get("kind", "")
    job_id = request.GET.get("job", "")
    q = request.GET.get("q", "").strip()

    errors = MigrationError.objects.select_related("job")
    if kind:
        errors = errors.filter(object_kind=kind)
    if job_id:
        errors = errors.filter(job_id=job_id)
    if q:
        errors = errors.filter(
            Q(object_name__icontains=q) | Q(message__icontains=q)
        )

    errors = errors[:500]
    context = {
        "errors": errors,
        "kind_choices": MigrationError.ObjectKind.choices,
        "kind": kind,
        "job_id": job_id,
        "q": q,
        "jobs": MigrationJob.objects.all()[:50],
        "kind_counts": {
            k: MigrationError.objects.filter(object_kind=k).count()
            for k, _ in MigrationError.ObjectKind.choices
        },
    }
    return render(request, "converter/errors.html", context)


def verify_view(request):
    """Live, read-only database comparison workspace."""
    jobs = MigrationJob.objects.select_related("source", "target").filter(
        status__in=[MigrationJob.Status.COMPLETED, MigrationJob.Status.PARTIAL]
    )
    selected = None
    job_id = request.GET.get("migration")
    if job_id:
        selected = get_object_or_404(jobs, pk=job_id)
    elif jobs:
        selected = jobs.first()
    return render(request, "converter/verify.html", {
        "jobs": jobs,
        "selected": selected,
        "sections": SECTION_LABELS,
    })


def verify_section_view(request, pk, section):
    """Return one live comparison section for progressive loading in the UI."""
    job = get_object_or_404(
        MigrationJob.objects.select_related("source", "target"), pk=pk,
    )
    try:
        result = compare_live(job.source, job.target, section)
        return JsonResponse(result)
    except (ConnectorError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": f"Verification query failed: {exc}"}, status=500)
