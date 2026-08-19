import logging
import os

from django.apps import AppConfig

logger = logging.getLogger("dialectbridge.web")


def _pid_is_alive(pid) -> bool:
    """Best-effort check whether a process with the given pid still runs."""
    if not pid:
        return False
    try:
        return os.path.isdir(f"/proc/{int(pid)}")
    except (OSError, ValueError):
        return False


class ConverterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.converter"