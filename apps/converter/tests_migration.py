"""End-to-end smoke test of the migration pipeline against in-memory connectors.

No live database is required: the fake connector implements the full
DatabaseConnector surface over plain Python dicts, so the orchestrator
(extract -> convert -> DDL -> data copy -> FK/objects -> verify) runs
exactly as it would against real MSSQL/PostgreSQL servers.
"""
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from engine.connectors.base import ConnectorError
from engine.connectors.mssql import MSSQLConnector
from engine.connectors.postgres import PostgresConnector
from engine.migration.data_mover import DataMigration
from engine.migration.orchestrator import MigrationOrchestrator
from engine.schema import Column, Constraint, Database, Index, Table, View
from engine.translators.sql_builder import build_table_ddl


class MSSQLConnectorTests(SimpleTestCase):
    @patch("engine.connectors.mssql.pymssql.connect")
    def test_connect_uses_datetime2_binding_to_preserve_microseconds(self, connect):
        raw_connection = Mock()
        connect.return_value = raw_connection
        connector = MSSQLConnector("db-host", 1433, "products", "user", "password")

        connector.connect()

        self.assertTrue(connect.call_args.kwargs["use_datetime2"])
        raw_connection.autocommit.assert_called_once_with(True)

    def test_keyless_table_streams_every_batch(self):
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        cursor.fetchmany.side_effect = [[(1,), (2,)], [(3,), (4,)], [(5,)], []]
        connection = Mock()
        connection.cursor.return_value = cursor
        connector = MSSQLConnector("host", 1433, "db", "user", "password")
        connector._conn = connection

        batches = list(connector.iter_table_rows("dbo.Log", ["Value"], [], batch_size=2))

        self.assertEqual(sum(len(batch) for batch in batches), 5)
        cursor.execute.assert_called_once_with("SELECT [Value] FROM [dbo].[Log]")

    def test_composite_key_uses_lexicographic_pagination_and_real_column_positions(self):
        connector = MSSQLConnector("host", 1433, "db", "user", "password")
        connector.fetch = Mock(side_effect=[[("first", 1, 2), ("second", 1, 3)], [("third", 2, 1)]])

        rows = list(connector.iter_table_rows(
            "dbo.Items", ["Payload", "TenantID", "ItemID"],
            ["TenantID", "ItemID"], batch_size=2,
        ))

        self.assertEqual(sum(len(batch) for batch in rows), 3)
        second_sql, second_params = connector.fetch.call_args_list[1].args
        self.assertIn("([TenantID] > %s) OR ([TenantID] = %s AND [ItemID] > %s)", second_sql)
        self.assertEqual(second_params, (1, 1, 3))


class PostgresConnectorPaginationTests(SimpleTestCase):
    def test_copy_streams_binary_as_bytea_without_raw_non_utf8_bytes(self):
        copied_chunks = []
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)

        def consume_copy(_sql, stream, size):
            self.assertEqual(size, 1024 * 1024)
            while chunk := stream.read(7):
                copied_chunks.append(chunk)

        cursor.copy_expert.side_effect = consume_copy
        connection = Mock()
        connection.cursor.return_value = cursor
        connector = PostgresConnector("host", 5432, "db", "user", "password")
        connector._conn = connection

        batches_consumed = []

        def rows():
            for batch in [[(1, b"\xff\xd8")], [(2, memoryview(b"\x00\x80"))]]:
                batches_consumed.append(batch)
                yield batch

        copied = connector.copy_to_table("dbo.Attachments", ["id", "payload"], rows())
        payload = "".join(copied_chunks)

        self.assertEqual(copied, 2)
        self.assertEqual(len(batches_consumed), 2)
        self.assertIn("1\t\\\\xffd8\n", payload)
        self.assertIn("2\t\\\\x0080\n", payload)
        self.assertNotIn("\xff", payload)

    def test_computed_column_downgrade_keeps_materialized_source_value_copyable(self):
        computed = Column(
            "DisplayName", "NVARCHAR(200)", is_computed=True,
            computed_definition="CONCAT([FirstName], [LastName])",
        )
        table = Table(name="dbo.People", columns=[Column("Id", "INT"), computed])

        statements, _warnings = build_table_ddl(
            table, "postgres", "tsql", downgrade_computed=True,
        )

        self.assertIn('"DisplayName" VARCHAR(200)', statements[0])
        self.assertNotIn("GENERATED ALWAYS", statements[0])
        self.assertFalse(computed.is_computed)

    def test_function_based_computed_column_is_downgraded_before_create_table(self):
        computed = Column(
            "DisplayName", "NVARCHAR(200)", is_computed=True,
            computed_definition="CONCAT([FirstName], ' ', [LastName])",
        )
        table = Table(name="dbo.People", columns=[
            Column("FirstName", "NVARCHAR(80)"),
            Column("LastName", "NVARCHAR(80)"),
            computed,
        ])

        statements, warnings = build_table_ddl(table, "postgres", "tsql")

        self.assertNotIn("GENERATED ALWAYS", statements[0])
        self.assertIn('"DisplayName" VARCHAR(200) DEFAULT NULL', statements[0])
        self.assertFalse(computed.is_computed)
        self.assertTrue(any("plain nullable column" in warning for warning in warnings), warnings)

    def test_pure_arithmetic_computed_column_remains_generated(self):
        computed = Column(
            "LineTotal", "DECIMAL(18,2)", is_computed=True,
            computed_definition="([Quantity] * [UnitPrice])",
        )
        table = Table(name="dbo.Lines", columns=[
            Column("Quantity", "INT"), Column("UnitPrice", "DECIMAL(18,2)"), computed,
        ])

        statements, _warnings = build_table_ddl(table, "postgres", "tsql")

        self.assertIn("GENERATED ALWAYS AS", statements[0])
        self.assertTrue(computed.is_computed)

    def test_copy_batch_size_is_large_for_narrow_tables_and_bounded_for_lobs(self):
        narrow = Table("dbo.Attendance", [Column("Id", "BIGINT"), Column("Day", "DATE")])
        wide = Table("dbo.Attachments", [Column("Id", "BIGINT"), Column("Payload", "VARBINARY(MAX)")])

        self.assertEqual(DataMigration(Mock(), Mock(), narrow).batch_size, 25_000)
        self.assertEqual(DataMigration(Mock(), Mock(), wide).batch_size, 5_000)

    def test_composite_key_uses_tuple_pagination_and_real_column_positions(self):
        connector = PostgresConnector("host", 5432, "db", "user", "password")
        connector.fetch = Mock(side_effect=[[("first", 1, 2), ("second", 1, 3)], [("third", 2, 1)]])

        rows = list(connector.iter_table_rows(
            "public.Items", ["Payload", "TenantID", "ItemID"],
            ["TenantID", "ItemID"], batch_size=2,
        ))

        self.assertEqual(sum(len(batch) for batch in rows), 3)
        second_sql, second_params = connector.fetch.call_args_list[1].args
        self.assertIn('("TenantID", "ItemID") > (%s, %s)', second_sql)
        self.assertEqual(second_params, (1, 3))


class _FakeConnector:
    """Minimal in-memory implementation of the connector surface."""

    dialect = "postgres"

    def __init__(self, tables: dict[str, list[tuple]], dialect: str = "postgres"):
        self.database = "fake"
        self.dialect = dialect
        self.data = {name: [list(r) for r in rows] for name, rows in tables.items()}
        self.created = []
        self.executed = []

    # -- execution ---------------------------------------------------------
    def connect(self):
        pass

    def close(self):
        pass

    def test(self):
        return "fake"

    def _server_version(self):
        return "fake"

    def execute(self, sql, params=None):
        if not self.dialect == "postgres":
            raise ConnectorError("no-op for source")
        self.executed.append(sql)
        if sql.lstrip().upper().startswith("CREATE TABLE"):
            name = sql.split(" ")[2].strip('"')
            self.created.append(name)
            self.data.setdefault(name, [])

    def execute_many(self, sql, params_list):
        self.executed.append(sql)
        for row in params_list:
            self.data.setdefault("target_rows", [])
            # append to the table named in the INSERT ... INTO "x"
            import re
            m = re.search(r'INTO\s+"?([\w.]+)"?\s*\(', sql)
            tbl = m.group(1) if m else "target_rows"
            self.data.setdefault(tbl, []).append(row)

    def fetch(self, sql, params=None):
        return []

    def fetchone(self, sql, params=None):
        return None

    # -- schema -------------------------------------------------------------
    def extract_schema(self):
        db = Database(name="fake", dialect=self.dialect)
        for name, rows in self.data.items():
            if name == "target_rows":
                continue
            pk_col = "id"
            db.tables.append(Table(
                name=name,
                columns=[Column(pk_col, "INT", nullable=False, is_identity=True),
                         Column("payload", "NVARCHAR(50)")],
                primary_key=Constraint(f"PK_{name}", [pk_col]),
            ))
        return db

    # -- data ---------------------------------------------------------------
    def iter_table_rows(self, table_name, columns, order_columns, batch_size, int_columns=None):
        rows = self.data.get(table_name, [])
        cols = {name: i for i, name in enumerate(["id", "payload"])}
        idx = [cols[c] for c in columns]
        ordered = sorted(rows, key=lambda r: r[cols[order_columns[0]]] if order_columns else 0)
        for i in range(0, len(ordered), batch_size):
            yield [tuple(r[j] for j in idx) for r in ordered[i:i + batch_size]]

    def count_rows(self, table_name):
        return len(self.data.get(table_name, []))

    def set_identity_insert(self, table_name, on):
        pass

    def max_value(self, table_name, column):
        rows = self.data.get(table_name, [])
        return max((r[0] for r in rows), default=0)

    def seed_identity(self, table_name, column):
        pass

    def quote_ident(self, name):
        return f'"{name}"'


class _FakeTsqlResetTarget(_FakeConnector):
    """T-SQL target that reports pre-existing objects to the reset routine."""

    def __init__(self):
        super().__init__({}, dialect="tsql")
        self.database_user = "migrator"
        self._table_loop_calls = 0

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql.lstrip().upper().startswith("CREATE TABLE"):
            name = sql.split(" ")[2].strip('[]"')
            self.data.setdefault(name, [])

    def fetch(self, sql, params=None):
        lower = sql.lower()
        if "sys.tables t" in lower and "top (1)" in lower:
            self._table_loop_calls += 1
            return [["[dbo].[users]"]] if self._table_loop_calls == 1 else []
        if "sys.objects o" in lower and "'so'" in lower:
            return [["[dbo].[order_seq]"]]
        if "sys.types t" in lower and "is_user_defined = 1" in lower:
            return [["[dbo].[EmailType]"]]
        if "sys.database_principals" in lower and "type in ('s', 'u')" in lower:
            return [["[dbo].[other_user]"]]
        if "sys.database_principals" in lower and "type = 'r'" in lower:
            return [["[dbo].[ProductReader]"]]
        return []


class MigrationPipelineSmokeTests(SimpleTestCase):
    def test_secondary_indexes_are_created_after_bulk_data_load(self):
        source = _FakeConnector({"dbo.users": [(1, "a"), (2, "b")]}, dialect="tsql")
        schema = source.extract_schema()
        schema.tables[0].indexes = [Index("IX_users_payload", ["payload"])]
        source.extract_schema = Mock(return_value=schema)
        target = _FakeConnector({}, dialect="postgres")

        MigrationOrchestrator(source, target, copy_data=True).run()

        insert_position = next(i for i, sql in enumerate(target.executed) if sql.startswith("INSERT"))
        index_position = next(i for i, sql in enumerate(target.executed) if sql.startswith("CREATE INDEX"))
        self.assertLess(insert_position, index_position)

    def test_full_pipeline_copies_rows_and_verifies_counts(self):
        source = _FakeConnector({"dbo.users": [(1, "a"), (2, "b"), (3, "c")]}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")

        report = MigrationOrchestrator(source, target, copy_data=True).run()

        self.assertTrue(report.success, report.warnings)
        self.assertEqual(report.summary["tables"], 1)
        self.assertEqual(report.summary["rows_copied"], 3)
        self.assertEqual(target.data["dbo.users"], [[1, "a"], [2, "b"], [3, "c"]])
        self.assertEqual(report.verification, [{
            "table": "dbo.users", "source_rows": 3, "target_rows": 3, "match": True,
        }])

    def test_batched_keyset_pagination_used(self):
        source = _FakeConnector({f"dbo.t{i}": [(i, str(i))] for i in range(3)}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")

        MigrationOrchestrator(source, target, copy_data=True).run()

        inserts = [s for s in target.executed if s.startswith("INSERT")]
        self.assertEqual(len(inserts), 3)
        for sql in inserts:
            self.assertIn("VALUES (%s, %s)", sql)

    def test_reset_target_drops_schemas_before_migrating(self):
        source = _FakeConnector({"dbo.users": [(1, "a")]}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")

        MigrationOrchestrator(source, target, copy_data=True, reset_target=True).run()

        drops = [s for s in target.executed if s.lstrip().upper().startswith("DROP SCHEMA")]
        self.assertEqual(drops, ['DROP SCHEMA IF EXISTS "dbo" CASCADE'])

    def test_reset_tsql_drops_sequences_types_users_roles(self):
        source = _FakeConnector({"dbo.users": [(1, "a")]}, dialect="tsql")
        target = _FakeTsqlResetTarget()

        MigrationOrchestrator(source, target, copy_data=False, reset_target=True).run()

        self.assertIn("DROP TABLE [dbo].[users]", target.executed)
        self.assertIn("DROP SEQUENCE [dbo].[order_seq]", target.executed)
        self.assertIn("DROP TYPE [dbo].[EmailType]", target.executed)
        self.assertIn("DROP USER [dbo].[other_user]", target.executed)
        self.assertIn("DROP ROLE [dbo].[ProductReader]", target.executed)
        self.assertNotIn("DROP USER [dbo].[migrator]", target.executed)


class _FakeViewFailureTarget(_FakeConnector):
    """Target that rejects every CREATE VIEW with a missing-relation error."""

    def __init__(self, missing: str):
        super().__init__({}, dialect="postgres")
        self.missing = missing

    def execute(self, sql, params=None):
        if sql.lstrip().upper().startswith("CREATE OR REPLACE VIEW"):
            raise ConnectorError(
                f'PostgreSQL execute failed: relation "{self.missing}" does not exist'
            )
        return super().execute(sql, params)


class OrphanObjectReportingTests(SimpleTestCase):
    """SQL Server keeps views/routines whose tables were dropped long ago.

    They can never be created on the target, so they must be reported as
    skipped instead of inflating the failure count of an otherwise clean run.
    """

    def _run(self, missing):
        source = _FakeConnector({"dbo.users": [(1, "a")]}, dialect="tsql")
        schema = source.extract_schema()
        schema.views = [View(name="dbo.Vw_Orphan",
                             definition="SELECT * FROM dbo.tbl_BeneficiaryDet")]
        source.extract_schema = Mock(return_value=schema)
        target = _FakeViewFailureTarget(missing)
        return MigrationOrchestrator(source, target, copy_data=False).run()

    def test_view_over_a_table_missing_from_the_source_is_skipped(self):
        report = self._run("dbo.tbl_beneficiarydet")
        view_result = next(r for r in report.schema_results if r.kind == "view")
        self.assertEqual(view_result.status, "skipped")
        self.assertIn("does not exist in the source database", view_result.detail)
        self.assertTrue(report.success)
        self.assertEqual(report.summary["schema_failed"], 0)

    def test_view_over_a_table_present_in_the_source_still_fails(self):
        """A genuinely broken migration must not be hidden by the skip path."""
        report = self._run("dbo.users")
        view_result = next(r for r in report.schema_results if r.kind == "view")
        self.assertEqual(view_result.status, "failed")
        self.assertFalse(report.success)
