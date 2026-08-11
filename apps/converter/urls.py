from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import ConvertSQLView, ConversionJobViewSet
from . import web_views

router = DefaultRouter()
router.register(r"jobs", ConversionJobViewSet, basename="job")

urlpatterns = [
    # --- API ---
    path("api/convert/", ConvertSQLView.as_view(), name="api-convert"),
    path("api/", include(router.urls)),

    # --- Web UI ---
    path("", web_views.convert_form_view, name="convert-form"),
    path("history/", web_views.history_view, name="history"),
]