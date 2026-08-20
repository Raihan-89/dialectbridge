import logging
from logging.handlers import RotatingFileHandler

from django.conf import settings
from django.test import SimpleTestCase


class FileLoggingConfigurationTests(SimpleTestCase):
    def test_project_log_directory_and_file_handler_are_configured(self):
        expected_log = settings.BASE_DIR / "logs" / "dialectbridge.log"
        handler_config = settings.LOGGING["handlers"]["application_file"]

        self.assertTrue(settings.LOG_DIR.is_dir())
        self.assertEqual(handler_config["filename"], expected_log)
        self.assertEqual(handler_config["class"], "logging.handlers.RotatingFileHandler")
        self.assertEqual(handler_config["maxBytes"], 10 * 1024 * 1024)
        self.assertEqual(handler_config["backupCount"], 5)

    def test_application_and_django_loggers_share_rotating_file_handler(self):
        expected_log = (settings.BASE_DIR / "logs" / "dialectbridge.log").resolve()

        for logger_name in ("dialectbridge", "django"):
            handlers = logging.getLogger(logger_name).handlers
            file_handlers = [h for h in handlers if isinstance(h, RotatingFileHandler)]

            self.assertEqual(len(file_handlers), 1)
            self.assertEqual(expected_log, settings.BASE_DIR.joinpath(file_handlers[0].baseFilename).resolve())
