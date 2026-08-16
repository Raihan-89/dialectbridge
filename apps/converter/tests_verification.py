from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from engine.schema import Column, Database, Table, View
from .verification_service import _objects, _pair, compare_live


class VerificationServiceTests(SimpleTestCase):
    def test_pairs_names_case_insensitively_and_ignores_schema_mapping(self):
        source = {"orders": {"name": "dbo.Orders", "detail": "4 columns"}}
        target = {"orders": {"name": "public.orders", "detail": "4 columns"}}
        self.assertEqual(_pair(source, target)[0]["status"], "match")

    def test_column_difference_is_reported(self):
        db = Database("source", "tsql", tables=[Table(
            "dbo.Orders", [Column("Id", "INT", nullable=False, is_identity=True)]
        )])
        values = _objects(db, "columns")
        changed = {**values["orders.id"], "nullable": True}
        self.assertEqual(_pair(values, {"orders.id": changed})[0]["status"], "different")

    @patch("apps.converter.verification_service.connector_for")
    def test_live_overview_closes_both_connectors(self, connector_for):
        source_db = Database("source", "tsql", tables=[Table("dbo.T", [])])
        target_db = Database("target", "postgres", tables=[Table("public.t", [])])
        source_connector = SimpleNamespace(extract_schema=lambda: source_db, close=lambda: None)
        target_connector = SimpleNamespace(extract_schema=lambda: target_db, close=lambda: None)
        connector_for.side_effect = [source_connector, target_connector]

        result = compare_live(object(), object(), "overview")

        self.assertTrue(result["all_match"])
        self.assertEqual(result["counts"]["match"], 8)
