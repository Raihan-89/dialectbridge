from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import ConvertSQLView, ConversionJobViewSet, DatabaseConnectionViewSet, MigrationJobViewSet
from . import web_views

router = DefaultRouter()
router.register(r"jobs", ConversionJobViewSet, basename="job")
router.register(r"connections", DatabaseConnectionViewSet, basename="connection")
router.register(r"migrations", MigrationJobViewSet, basename="migration")

urlpatterns = [
    # --- API ---
    path("api/convert/", ConvertSQLView.as_view(), name="api-convert"),
    path("api/", include(router.urls)),

    # --- Web UI ---
    path("", web_views.convert_form_view, name="convert-form"),
    path("history/", web_views.history_view, name="history"),
    path("history/<int:pk>/delete/", web_views.conversion_delete_view, name="conversion-delete"),
    path("connections/", web_views.connections_view, name="connections"),
    path("migrate/", web_views.migrate_view, name="migrate"),
    path("migrate/<int:pk>/", web_views.migrate_detail_view, name="migrate-detail"),
    path("migrate/<int:pk>/delete/", web_views.migration_delete_view, name="migration-delete"),
    path("migrate/<int:pk>/status/", web_views.migrate_status_view, name="migrate-status"),
    path("errors/", web_views.errors_view, name="errors"),
    path("verify/", web_views.verify_view, name="verify"),
    path("verify/<int:pk>/<str:section>/", web_views.verify_section_view, name="verify-section"),
]
