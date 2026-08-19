import logging
import os

from django.apps import AppConfig

logger = logging.getLogger("dialectbridge.web")


def _pid_is_alive(pid) -> bool:
    """Best-effort cross-platform check whether a process with the given pid runs.

    Works on Linux via /proc, and on Windows via a signal-0 probe (no /proc
    exists there, so /proc-based checks would wrongly report everything dead).
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return True
    try:
        if os.path.isdir(f"/proc/{pid}"):
            return True
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but is protected from signaling.
        return True
    except OSError:
        # No such process (or signals unsupported) → treat as dead.
        return False


class ConverterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.converter"