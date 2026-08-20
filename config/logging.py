"""Project logging handlers."""
from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path


class DailyFileHandler(logging.FileHandler):
    """Write each local calendar day to its own date-named log file."""

    def __init__(self, directory, filename_prefix="dialectbridge", backupCount=30,
                 encoding="utf-8", errors=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.filename_prefix = filename_prefix
        self.backupCount = max(0, int(backupCount))
        self._active_date = self._today()
        super().__init__(
            self._path_for(self._active_date), mode="a", encoding=encoding,
            delay=True, errors=errors,
        )
        self._remove_expired_files()

    @staticmethod
    def _today():
        # Match logging.Formatter's default local-time timestamps.
        return datetime.now().astimezone().date()

    def _path_for(self, day) -> Path:
        return self.directory / f"{self.filename_prefix}-{day.isoformat()}.log"

    def emit(self, record):
        today = self._today()
        if today != self._active_date:
            if self.stream:
                self.stream.close()
                self.stream = None
            self._active_date = today
            self.baseFilename = str(self._path_for(today).resolve())
            self._remove_expired_files()
        super().emit(record)

    def _remove_expired_files(self) -> None:
        if not self.backupCount:
            return
        files = sorted(
            self.directory.glob(f"{self.filename_prefix}-????-??-??.log"),
            key=lambda path: path.name,
            reverse=True,
        )
        for expired in files[self.backupCount:]:
            try:
                expired.unlink()
            except OSError:
                # A log file held by another process is retained and retried
                # during a later rollover instead of breaking application logs.
                pass
