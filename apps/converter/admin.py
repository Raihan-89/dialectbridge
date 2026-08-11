from django.contrib import admin
from .models import ConversionJob


@admin.register(ConversionJob)
class ConversionJobAdmin(admin.ModelAdmin):
    list_display = ("id", "direction", "statement_type", "succeeded", "created_at", "created_by")
    list_filter = ("direction", "statement_type", "succeeded")
    readonly_fields = ("created_at",)
    search_fields = ("source_sql", "converted_sql")