import logging
from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from config.logging import DailyFileHandler, ExcludeMigrationStatusPolls


class FileLoggingConfigurationTests(SimpleTestCase):
    def test_project_log_directory_and_file_handler_are_configured(self):
        handler_config = settings.LOGGING["handlers"]["application_file"]

        self.assertTrue(settings.LOG_DIR.is_dir())
        self.assertEqual(handler_config["directory"], settings.LOG_DIR)
        self.assertEqual(handler_config["class"], "config.logging.DailyFileHandler")
        self.assertEqual(handler_config["filename_prefix"], "dialectbridge")
        self.assertEqual(handler_config["backupCount"], 30)
        self.assertEqual(handler_config["filters"], ["exclude_migration_status_polls"])

    def test_migration_status_poll_is_excluded_from_file_logs(self):
        log_filter = ExcludeMigrationStatusPolls()
        poll = logging.LogRecord(
            "django.server", logging.INFO, __file__, 1,
            '"GET /migrate/7/status/ HTTP/1.1" 200 11747', (), None,
        )
        migration = logging.LogRecord(
            "dialectbridge.migration", logging.INFO, __file__, 1,
            "Table copied table=dbo.p2_finger_template rows=309350", (), None,
        )

        self.assertFalse(log_filter.filter(poll))
        self.assertTrue(log_filter.filter(migration))

    def test_application_and_django_loggers_share_daily_file_handler(self):
        expected_name = f"dialectbridge-{date.today().isoformat()}.log"

        for logger_name in ("dialectbridge", "django"):
            handlers = logging.getLogger(logger_name).handlers
            file_handlers = [h for h in handlers if isinstance(h, DailyFileHandler)]

            self.assertEqual(len(file_handlers), 1)
            self.assertEqual(expected_name, file_handlers[0]._path_for(file_handlers[0]._active_date).name)

    def test_handler_switches_to_a_new_date_named_file(self):
        handler = DailyFileHandler(settings.LOG_DIR, filename_prefix="daily-test", backupCount=2)
        handler._active_date = date(2026, 8, 19)
        handler.baseFilename = str(handler._path_for(handler._active_date))

        with patch.object(handler, "_today", return_value=date(2026, 8, 20)):
            handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None))

        self.assertEqual(handler._path_for(date(2026, 8, 20)).name, "daily-test-2026-08-20.log")
        self.assertTrue(handler._path_for(date(2026, 8, 20)).exists())
        handler.close()
        handler._path_for(date(2026, 8, 20)).unlink(missing_ok=True)
