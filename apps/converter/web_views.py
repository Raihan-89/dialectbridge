from django.shortcuts import render

from engine.translators.ddl_translator import convert_ddl
from .models import ConversionJob
from .views import DIRECTION_TO_DIALECTS


def convert_form_view(request):
    """
    Simple HTML form: paste SQL in, pick a direction, see converted SQL +
    warnings on the same page. Reuses the exact same engine call as the
    API view, and also saves a ConversionJob record — so conversions done
    via the web UI show up in history too.
    """
    context = {"direction_choices": ConversionJob.Direction.choices}

    if request.method == "POST":
        source_sql = request.POST.get("source_sql", "").strip()
        direction = request.POST.get("direction")
        read_dialect, write_dialect = DIRECTION_TO_DIALECTS[direction]

        job = ConversionJob(
            direction=direction,
            statement_type=ConversionJob.StatementType.DDL,
            source_sql=source_sql,
            created_by=request.user if request.user.is_authenticated else None,
        )

        try:
            result = convert_ddl(source_sql, source_dialect=read_dialect, target_dialect=write_dialect)
            job.converted_sql = result.sql
            job.warnings = result.warnings
            job.succeeded = True
        except Exception as exc:
            job.succeeded = False
            job.error_message = str(exc)
        finally:
            job.save()

        context.update({
            "source_sql": source_sql,
            "direction": direction,
            "job": job,
        })

    return render(request, "converter/convert_form.html", context)


def history_view(request):
    """Lists past conversion jobs, most recent first."""
    jobs = ConversionJob.objects.all()[:50]
    return render(request, "converter/history.html", {"jobs": jobs})