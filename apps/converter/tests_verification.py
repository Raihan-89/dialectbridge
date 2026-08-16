from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from engine.schema import Column, Constraint, Database, Table, View
from .verification_service import (
    _objects, _pair, compare_live, compare_live_table_data, list_live_data_tables,
)


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

    @patch("apps.converter.verification_service.connector_for")
    def test_lists_shared_and_one_sided_data_tables(self, connector_for):
        source_db = Database("source", "postgres", tables=[Table("public.Products", []), Table("public.Legacy", [])])
        target_db = Database("target", "tsql", tables=[Table("dbo.products", [])])
        connector_for.side_effect = [
            SimpleNamespace(extract_schema=lambda: source_db, close=lambda: None),
            SimpleNamespace(extract_schema=lambda: target_db, close=lambda: None),
        ]

        result = list_live_data_tables(object(), object())

        self.assertEqual([table["key"] for table in result["tables"]], ["legacy", "products"])
        self.assertEqual(result["tables"][0]["status"], "source_only")
        self.assertEqual(result["tables"][1]["status"], "both")

    @patch("apps.converter.verification_service.connector_for")
    def test_compares_table_rows_by_shared_primary_key(self, connector_for):
        source_table = Table("public.Products", [Column("Id", "integer"), Column("Name", "text")], Constraint("products_pkey", ["Id"]))
        target_table = Table("dbo.products", [Column("id", "int"), Column("name", "nvarchar")], Constraint("PK_products", ["id"]))
        source_db = Database("source", "postgres", tables=[source_table])
        target_db = Database("target", "tsql", tables=[target_table])

        def fake(db, dialect, rows):
            return SimpleNamespace(
                dialect=dialect, extract_schema=lambda: db, fetch=lambda sql: rows,
                count_rows=lambda name: len(rows), close=lambda: None,
                quote_ident=lambda name: ".".join(f'[{part}]' for part in name.split(".")) if dialect == "tsql" else ".".join(f'"{part}"' for part in name.split(".")),
            )

        connector_for.side_effect = [
            fake(source_db, "postgres", [(1, "Pen"), (2, "Book")]),
            fake(target_db, "tsql", [(1, "Pen"), (2, "Notebook")]),
        ]

        result = compare_live_table_data(object(), object(), "products")

        self.assertTrue(result["has_shared_pk"])
        self.assertEqual(result["counts"]["match"], 1)
        self.assertEqual(result["counts"]["different"], 1)
        self.assertEqual(result["rows"][1]["target"]["name"], "Notebook")
