import threading
import uuid
from datetime import timedelta

from django.contrib import messages
from django.db import close_old_connections
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from engine.connectors.base import ConnectorError
from engine.service import convert_sql, UnsupportedStatementTypeError
from . import migration_service
from .verification_service import (
    SECTION_LABELS, compare_live, compare_live_table_data, list_live_data_tables,
)
from .models import ConversionJob, DatabaseConnection, MigrationError, MigrationJob

# Statement types the current engine can actually handle — others are shown
# disabled in the form until their translators land.
SUPPORTED_STATEMENT_TYPES = {"ddl", "dml"}
DELETE_UNDO_SECONDS = 5


def _finalize_migration_deletion(token: str) -> None:
    """Permanently remove a pending batch once its undo window expires."""
    close_old_connections()
    try:
        MigrationJob.objects.filter(
            pending_deletion_token=token,
            pending_deletion_at__lte=timezone.now(),
        ).delete()
    finally:
        close_old_connections()


def _schedule_migration_deletion(token: str) -> None:
    timer = threading.Timer(DELETE_UNDO_SECONDS + 0.1, _finalize_migration_deletion, args=(token,))
    timer.daemon = True
    timer.start()


def _queue_migration_deletion(request, jobs, label: str) -> int:
    ids = list(jobs.values_list("pk", flat=True))
    if not ids:
        return 0
    token = str(uuid.uuid4())
    delete_at = timezone.now() + timedelta(seconds=DELETE_UNDO_SECONDS)
    MigrationJob.objects.filter(pk__in=ids).update(
        pending_deletion_token=token, pending_deletion_at=delete_at,
    )
    request.session["migration_delete_undo"] = {
        "token": token, "label": label, "count": len(ids),
        "deadline_ms": int(delete_at.timestamp() * 1000),
    }
    _schedule_migration_deletion(token)
    return len(ids)


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
    jobs = ConversionJob.objects.order_by("-created_at", "-pk")[:50]
    return render(request, "converter/history.html", {"jobs": jobs})


@require_POST
def conversion_delete_view(request, pk):
    """Delete one saved SQL conversion after the UI confirmation step."""
    job = get_object_or_404(ConversionJob, pk=pk)
    job.delete()
    messages.success(request, "Conversion history entry deleted.")
    return redirect("history")


@require_POST
def conversion_bulk_delete_view(request):
    ids = request.POST.getlist("selected_ids")
    deleted, _ = ConversionJob.objects.filter(pk__in=ids).delete() if ids else (0, {})
    if deleted:
        messages.success(request, f"Deleted {deleted} conversion history entries.")
    else:
        messages.warning(request, "Select at least one conversion to delete.")
    return redirect("history")


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
    # Clean up expired batches after a process restart, when the original
    # in-memory finalizer timer no longer exists.
    MigrationJob.objects.filter(pending_deletion_at__lte=timezone.now()).delete()
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

    undo = request.session.get("migration_delete_undo")
    if undo and undo.get("deadline_ms", 0) <= int(timezone.now().timestamp() * 1000):
        request.session.pop("migration_delete_undo", None)
        undo = None
    context = {
        "connections": DatabaseConnection.objects.all(),
        "recent_jobs": MigrationJob.objects.filter(pending_deletion_token__isnull=True).order_by("-created_at", "-pk"),
        "running_job": MigrationJob.objects.filter(status=MigrationJob.Status.RUNNING).first(),
        "undo_deletion": undo,
    }
    return render(request, "converter/migrate.html", context)


def migrate_detail_view(request, pk):
    job = get_object_or_404(MigrationJob, pk=pk, pending_deletion_token__isnull=True)
    return render(request, "converter/migrate_detail.html", {"job": job})


@require_POST
def migration_delete_view(request, pk):
    """Delete a finished migration report and its cascading captured errors."""
    job = get_object_or_404(MigrationJob, pk=pk)
    if job.status in {MigrationJob.Status.RUNNING, MigrationJob.Status.PENDING}:
        messages.error(request, "A running or pending migration cannot be deleted.")
        return redirect("migrate-detail", pk=job.pk)
    label = job.name or f"Migration #{job.pk}"
    _queue_migration_deletion(request, MigrationJob.objects.filter(pk=job.pk), label)
    return redirect("migrate")


@require_POST
def migration_bulk_delete_view(request):
    ids = request.POST.getlist("selected_ids")
    jobs = MigrationJob.objects.filter(pk__in=ids).exclude(
        status__in=[MigrationJob.Status.RUNNING, MigrationJob.Status.PENDING]
    )
    selected_count = len(set(ids))
    job_count = jobs.count()
    if job_count:
        label = f"{job_count} migrations"
        _queue_migration_deletion(request, jobs, label)
    else:
        messages.warning(request, "Select at least one finished migration to delete.")
    if selected_count > job_count:
        messages.warning(request, "Running or pending migrations were kept.")
    return redirect("migrate")


@require_POST
def migration_undo_delete_view(request):
    token = request.POST.get("token", "")
    try:
        uuid.UUID(token)
    except (ValueError, AttributeError):
        messages.warning(request, "This undo request is invalid or has expired.")
        return redirect("migrate")
    restored = MigrationJob.objects.filter(
        pending_deletion_token=token,
        pending_deletion_at__gt=timezone.now(),
    ).update(pending_deletion_token=None, pending_deletion_at=None)
    request.session.pop("migration_delete_undo", None)
    if restored:
        messages.success(request, f"Restored {restored} migration history entr{'y' if restored == 1 else 'ies'}.")
    else:
        _finalize_migration_deletion(token)
        messages.warning(request, "The five-second undo window has expired.")
    return redirect("migrate")


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
        status__in=[MigrationJob.Status.COMPLETED, MigrationJob.Status.PARTIAL],
        pending_deletion_token__isnull=True,
    ).order_by("-created_at", "-pk")
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


def data_compare_view(request):
    """Read-only browser for comparing migrated table records."""
    jobs = MigrationJob.objects.select_related("source", "target").filter(
        status__in=[MigrationJob.Status.COMPLETED, MigrationJob.Status.PARTIAL],
        pending_deletion_token__isnull=True,
    ).order_by("-created_at", "-pk")
    selected = None
    job_id = request.GET.get("migration")
    if job_id:
        selected = get_object_or_404(jobs, pk=job_id)
    elif jobs:
        selected = jobs.first()
    return render(request, "converter/data_compare.html", {"jobs": jobs, "selected": selected})


@require_GET
def data_compare_tables_view(request, pk):
    job = get_object_or_404(
        MigrationJob.objects.select_related("source", "target"), pk=pk,
        status__in=[MigrationJob.Status.COMPLETED, MigrationJob.Status.PARTIAL],
        pending_deletion_token__isnull=True,
    )
    try:
        return JsonResponse(list_live_data_tables(job.source, job.target))
    except ConnectorError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": f"Table discovery failed: {exc}"}, status=500)


@require_GET
def data_compare_rows_view(request, pk):
    job = get_object_or_404(
        MigrationJob.objects.select_related("source", "target"), pk=pk,
        status__in=[MigrationJob.Status.COMPLETED, MigrationJob.Status.PARTIAL],
        pending_deletion_token__isnull=True,
    )
    table = request.GET.get("table", "").strip().casefold()
    try:
        page = int(request.GET.get("page", 1))
        return JsonResponse(compare_live_table_data(job.source, job.target, table, page=page))
    except (ConnectorError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({"error": f"Data comparison failed: {exc}"}, status=500)
