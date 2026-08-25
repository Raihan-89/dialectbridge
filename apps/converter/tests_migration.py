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
        row = report.verification[0]
        self.assertEqual(
            {k: row[k] for k in ("table", "source_rows", "target_rows", "match")},
            {"table": "dbo.users", "source_rows": 3, "target_rows": 3, "match": True},
        )
        self.assertIsNotNone(row["duration_seconds"])

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


class CopyStreamRenderingTests(SimpleTestCase):
    """The COPY TEXT renderer is the hot path of every migration; these pin the
    escaping contract so speed work can never corrupt a value."""

    def _render(self, batches):
        from engine.connectors.postgres import _CopyTextStream
        stream = _CopyTextStream(batches)
        return stream.read(), stream.rows_read

    def test_reserved_characters_are_escaped_and_null_becomes_backslash_n(self):
        text, rows = self._render([[["a\tb", "c\nd", "e\\f", None, "plain"]]])
        self.assertEqual(text, "a\\tb\tc\\nd\te\\\\f\t\\N\tplain\n")
        self.assertEqual(rows, 1)

    def test_carriage_return_is_escaped(self):
        text, _ = self._render([[["a\rb"]]])
        self.assertEqual(text, "a\\rb\n")

    def test_binary_values_keep_the_double_escaped_hex_form(self):
        text, _ = self._render([[[b"\xff\x00"]]])
        self.assertEqual(text, "\\\\xff00\n")

    def test_numbers_dates_and_decimals_round_trip_as_text(self):
        import datetime
        from decimal import Decimal
        text, _ = self._render([[[1, 2.5, Decimal("3.40"),
                                  datetime.datetime(2020, 1, 2, 3, 4, 5), True]]])
        self.assertEqual(text, "1\t2.5\t3.40\t2020-01-02 03:04:05\tTrue\n")

    def test_multiple_batches_and_empty_batches_stream_every_row(self):
        batches = [[[1], [2]], [], [[3]]]
        text, rows = self._render(batches)
        self.assertEqual(text, "1\n2\n3\n")
        self.assertEqual(rows, 3)

    def test_chunked_reads_reassemble_the_same_payload(self):
        from engine.connectors.postgres import _CopyTextStream
        batches = [[[f"row-{i}", i] for i in range(500)],
                   [[f"row-{i}", i] for i in range(500, 900)]]
        expected, _ = self._render([list(b) for b in batches])
        stream = _CopyTextStream([list(b) for b in batches])
        out = []
        while True:
            chunk = stream.read(64)
            if not chunk:
                break
            out.append(chunk)
        self.assertEqual("".join(out), expected)
        self.assertEqual(stream.rows_read, 900)


class ParallelCopyTests(SimpleTestCase):
    """Copying tables in worker processes must stay strictly optional: any
    connector that cannot be rebuilt in a child falls back to the original
    single-connection loop rather than failing the migration."""

    def test_connector_spec_rejects_a_connector_it_cannot_rebuild(self):
        from engine.migration.parallel_copy import ParallelCopyUnavailable, connector_spec
        with self.assertRaises(ParallelCopyUnavailable):
            connector_spec(_FakeConnector({}, dialect="tsql"))

    def test_connector_spec_captures_real_connection_parameters(self):
        from engine.migration.parallel_copy import connector_spec
        connector = PostgresConnector(host="h", port=5432, database="d",
                                      user="u", password="p")
        module, qualname, host, port, database, user, password = connector_spec(connector)
        self.assertEqual((module, qualname), ("engine.connectors.postgres", "PostgresConnector"))
        self.assertEqual((host, port, database, user, password), ("h", 5432, "d", "u", "p"))

    def test_unusable_connectors_fall_back_to_the_sequential_copy(self):
        source = _FakeConnector({"dbo.a": [(1, "x")], "dbo.b": [(2, "y")]}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")

        report = MigrationOrchestrator(source, target, copy_data=True,
                                       parallel_workers=4).run()

        self.assertTrue(report.success)
        self.assertEqual({r.name for r in report.data_results}, {"dbo.a", "dbo.b"})
        self.assertEqual(sum(r.rows_copied for r in report.data_results), 2)

    def test_default_worker_count_is_bounded_and_overridable(self):
        from apps.converter.migration_service import _default_copy_workers
        self.assertGreaterEqual(_default_copy_workers(), 2)
        self.assertLessEqual(_default_copy_workers(), 8)
        with patch.dict("os.environ", {"DIALECTBRIDGE_COPY_WORKERS": "7"}):
            self.assertEqual(_default_copy_workers(), 7)
        with patch.dict("os.environ", {"DIALECTBRIDGE_COPY_WORKERS": "nonsense"}):
            self.assertLessEqual(_default_copy_workers(), 8)

    def test_a_single_worker_keeps_the_original_sequential_path(self):
        source = _FakeConnector({"dbo.a": [(1, "x")]}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")
        orchestrator = MigrationOrchestrator(source, target, copy_data=True)
        self.assertEqual(orchestrator.parallel_workers, 1)
        with patch.object(orchestrator, "_copy_tables_parallel") as parallel:
            orchestrator.run()
        parallel.assert_not_called()

    def test_worker_failure_is_reported_against_the_table_not_raised(self):
        """A crash inside a worker must surface as a failed table."""
        source = _FakeConnector({"dbo.a": [(1, "x")], "dbo.b": [(2, "y")]}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")
        orchestrator = MigrationOrchestrator(source, target, copy_data=True,
                                             parallel_workers=2)

        def _fake_copy(src, tgt, tables, workers, on_progress, on_done):
            for table in tables:
                on_done(table.name, None, "ConnectorError: boom")

        with patch("engine.migration.parallel_copy.copy_tables", _fake_copy):
            report = orchestrator.run()

        self.assertFalse(report.success)
        failed = [r for r in report.data_results if r.status == "failed"]
        self.assertEqual(len(failed), 2)
        self.assertIn("boom", failed[0].detail)


class TableDurationReportingTests(SimpleTestCase):
    """The UI showed the running table as `29685080m 17s`: it subtracted a
    monotonic() reading from the browser's epoch clock. The elapsed time is now
    computed where the monotonic base is known."""

    def _capture(self):
        samples = []

        def callback(pct, stage, data=None):
            for name, entry in ((data or {}).get("table_progress") or {}).items():
                if "elapsed_seconds" in entry:
                    samples.append((name, entry["elapsed_seconds"], entry.get("done")))

        source = _FakeConnector({"dbo.a": [(1, "x")], "dbo.b": [(2, "y")]}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")
        MigrationOrchestrator(source, target, copy_data=True,
                              progress_callback=callback).run()
        return samples

    def test_every_table_reports_a_plausible_duration(self):
        samples = self._capture()
        self.assertTrue(samples, "no per-table elapsed times were emitted")
        self.assertEqual({name for name, _, _ in samples}, {"dbo.a", "dbo.b"})
        for name, elapsed, _done in samples:
            self.assertGreaterEqual(elapsed, 0.0, name)
            # A monotonic/epoch mix-up lands around 1.7e9 seconds.
            self.assertLess(elapsed, 600, f"{name} reported {elapsed}s")

    def test_finished_tables_freeze_their_duration(self):
        samples = self._capture()
        done = [(n, e) for n, e, d in samples if d]
        self.assertTrue(done)
        for name, elapsed in done:
            self.assertLess(elapsed, 600, f"{name} reported {elapsed}s")

    def test_elapsed_is_the_finished_span_once_a_table_is_done(self):
        progress = {
            "dbo.a": {"table_started": 100.0, "table_finished": 142.5, "done": True},
            "dbo.b": {"table_started": 200.0, "done": False},
        }
        MigrationOrchestrator._stamp_elapsed(progress)
        self.assertEqual(progress["dbo.a"]["elapsed_seconds"], 42.5)
        self.assertGreaterEqual(progress["dbo.b"]["elapsed_seconds"], 0.0)

    def test_entries_without_a_start_are_left_alone(self):
        progress = {"dbo.a": {"rows_copied": 0}}
        MigrationOrchestrator._stamp_elapsed(progress)
        self.assertNotIn("elapsed_seconds", progress["dbo.a"])

class CopyDurationTests(SimpleTestCase):
    """Per-table copy times survive into the stored report.

    While a migration runs the report page shows each table's elapsed time from
    the live progress payload. That payload is gone once the job finishes, so
    the finished report used to show no timings at all.
    """

    def _report(self):
        source = _FakeConnector({"dbo.users": [(1, "a"), (2, "b")]}, dialect="tsql")
        target = _FakeConnector({}, dialect="postgres")
        return MigrationOrchestrator(source, target, copy_data=True).run()

    def test_every_copied_table_records_its_duration(self):
        report = self._report()
        result = report.data_results[0].to_dict()
        self.assertIsNotNone(result["duration_seconds"])
        self.assertGreaterEqual(result["duration_seconds"], 0.0)
        self.assertTrue(result["duration_display"])

    def test_verification_rows_carry_the_same_duration(self):
        report = self._report()
        self.assertEqual(
            report.verification[0]["duration_seconds"],
            report.data_results[0].duration_seconds,
        )

    def test_summary_reports_the_wall_clock_copy_time(self):
        report = self._report()
        self.assertIn("data_seconds", report.summary)
        self.assertGreaterEqual(report.summary["data_seconds"], 0.0)
        self.assertTrue(report.summary["data_duration"])

    def test_durations_are_formatted_for_humans(self):
        from engine.migration.orchestrator import format_duration

        self.assertEqual(format_duration(None), "\u2014")
        self.assertEqual(format_duration(0.42), "0.42s")
        self.assertEqual(format_duration(42.5), "42.5s")
        self.assertEqual(format_duration(82), "1m 22s")
        self.assertEqual(format_duration(3723), "1h 02m 03s")


class TableShardPlanningTests(SimpleTestCase):
    """A migration is no faster than its biggest table unless that table is
    itself split across workers. Splitting must only happen where it is
    provably safe: a single integer primary key and enough rows to pay for it.
    """

    def _table(self, pk_columns=("Id",), pk_type="BIGINT", extra=()):
        columns = [Column(name, pk_type) for name in pk_columns]
        columns.extend(extra)
        return Table(
            name="dbo.Big", columns=columns,
            primary_key=Constraint(name="pk", columns=list(pk_columns)),
        )

    def _plan(self, table, rows, bounds=(1, 20_000_000), size_bytes=0):
        from engine.migration.table_shards import plan_shards
        source = Mock()
        source.key_bounds.return_value = bounds
        return plan_shards(source, [table], {table.name.lower(): (rows, size_bytes)})

    def test_a_large_single_integer_key_table_is_split(self):
        plan = self._plan(self._table(), 19_000_000)
        key, ranges = plan["dbo.Big"]
        self.assertEqual(key, "Id")
        self.assertGreater(len(ranges), 1)

    def test_ranges_are_contiguous_and_cover_everything(self):
        """Gaps would silently drop rows; overlaps would duplicate them."""
        _key, ranges = self._plan(self._table(), 19_000_000)["dbo.Big"]
        self.assertIsNone(ranges[0][0])          # no lower bound on the first
        self.assertIsNone(ranges[-1][1])         # no upper bound on the last
        for (_lo, high), (low, _hi) in zip(ranges, ranges[1:]):
            self.assertEqual(high, low)          # each ends exactly where the next begins

    def test_every_key_lands_in_exactly_one_slice(self):
        """The invariant the whole feature rests on. A gap silently drops rows
        and an overlap silently duplicates them — neither would fail loudly."""
        from engine.migration.table_shards import shard_count, split_range
        cases = {
            "dense": list(range(1, 20_001)),
            "sparse": list(range(1, 5_000_000, 227)),
            "negative": list(range(-40_000, 40_000, 3)),
            # 90% of the keys bunched at one end, as after a bulk backfill.
            "skewed": list(range(1, 2_000)) + list(range(900_000, 920_000)),
        }
        for label, keys in cases.items():
            with self.subTest(label):
                ranges = split_range(min(keys), max(keys), shard_count(19_000_000))
                for key in keys:
                    hits = sum(
                        1 for low, high in ranges
                        if (low is None or key >= low) and (high is None or key < high)
                    )
                    self.assertEqual(hits, 1, f"{label} key {key} matched {hits} slices")

    def test_keys_outside_the_sampled_bounds_are_still_covered(self):
        """Rows inserted while the migration runs can fall outside the MIN/MAX
        sampled up front; the open-ended first and last slices must catch them."""
        from engine.migration.table_shards import split_range
        ranges = split_range(1, 200_000, 8)
        for key in (-99, 0, 10 ** 12):
            hits = sum(
                1 for low, high in ranges
                if (low is None or key >= low) and (high is None or key < high)
            )
            self.assertEqual(hits, 1, key)

    def test_a_small_table_is_never_split(self):
        self.assertEqual(self._plan(self._table(), 5_000), {})

    def test_a_composite_key_is_never_split(self):
        self.assertEqual(self._plan(self._table(("TenantId", "Id")), 19_000_000), {})

    def test_a_non_integer_key_is_never_split(self):
        """Ranges over a UUID or text key would depend on collation."""
        self.assertEqual(
            self._plan(self._table(pk_type="UNIQUEIDENTIFIER"), 19_000_000), {},
        )

    def test_a_table_with_no_primary_key_is_never_split(self):
        table = Table(name="dbo.Big", columns=[Column("Payload", "NVARCHAR(50)")])
        self.assertEqual(self._plan(table, 19_000_000), {})

    def test_unreadable_key_bounds_fall_back_to_a_whole_table_copy(self):
        from engine.migration.table_shards import plan_shards
        source = Mock()
        source.key_bounds.side_effect = RuntimeError("catalog unavailable")
        self.assertEqual(
            plan_shards(source, [self._table()], {"dbo.big": (19_000_000, 0)}), {},
        )

    def test_estimates_are_optional(self):
        from engine.migration.table_shards import estimates_for
        self.assertEqual(estimates_for(object()), {})
        broken = Mock()
        broken.approx_table_stats.side_effect = RuntimeError("no catalog")
        self.assertEqual(estimates_for(broken), {})

    def test_a_big_blob_table_is_split_despite_a_small_row_count(self):
        """The measured run: 2,451 attachment rows took 91s at 27 rows/s while
        18.9M narrow rows took 42s. Row count alone left every blob table whole
        and they became the critical path."""
        plan = self._plan(self._table(), rows=2_451, size_bytes=3 * 1024 ** 3)
        self.assertIn("dbo.Big", plan)
        self.assertGreater(len(plan["dbo.Big"][1]), 1)

    def test_a_small_table_is_not_split_by_either_measure(self):
        self.assertEqual(self._plan(self._table(), rows=5_000, size_bytes=8 * 1024 ** 2), {})

    def test_slice_count_takes_the_larger_of_rows_and_bytes(self):
        from engine.migration.table_shards import shard_count
        self.assertEqual(shard_count(0, 0), 2)                       # never zero
        self.assertGreater(shard_count(2_451, 3 * 1024 ** 3), 2)     # sized by bytes
        self.assertGreater(shard_count(19_000_000, 0), 2)            # sized by rows


class ShardedRangeReadTests(SimpleTestCase):
    """A slice must read only its own key range."""

    def test_mssql_range_predicate_is_applied_and_bound(self):
        connector = MSSQLConnector("host", 1433, "db", "user", "password")
        connector.fetch = Mock(return_value=[])
        list(connector.iter_table_rows(
            "dbo.Big", ["Id", "Payload"], ["Id"], batch_size=100,
            key_range=("Id", 500, 900),
        ))
        sql, params = connector.fetch.call_args.args
        self.assertIn("[Id] >= %s", sql)
        self.assertIn("[Id] < %s", sql)
        self.assertEqual(params, (500, 900))

    def test_mssql_range_survives_into_the_keyset_page(self):
        """The second page still has to stay inside the slice."""
        connector = MSSQLConnector("host", 1433, "db", "user", "password")
        connector.fetch = Mock(side_effect=[[(1, "a"), (2, "b")], []])
        list(connector.iter_table_rows(
            "dbo.Big", ["Id", "Payload"], ["Id"], batch_size=2,
            key_range=("Id", 0, 900),
        ))
        sql, params = connector.fetch.call_args_list[1].args
        self.assertIn("[Id] > %s", sql)
        self.assertIn("[Id] < %s", sql)
        self.assertEqual(params, (2, 0, 900))

    def test_postgres_range_predicate_is_applied_and_bound(self):
        connector = PostgresConnector("host", 5432, "db", "user", "password")
        connector.fetch = Mock(return_value=[])
        list(connector.iter_table_rows(
            "dbo.Big", ["Id"], ["Id"], batch_size=100, key_range=("Id", 500, None),
        ))
        sql, params = connector.fetch.call_args.args
        self.assertIn('"Id" >= %s', sql)
        self.assertNotIn('"Id" <', sql)
        self.assertEqual(params, (500,))

    def test_an_unsliced_read_is_unchanged(self):
        """Whole-table reads must keep calling fetch exactly as they always did,
        so connectors that predate key ranges keep working."""
        connector = PostgresConnector("host", 5432, "db", "user", "password")
        connector.fetch = Mock(return_value=[])
        list(connector.iter_table_rows("dbo.T", ["Id"], ["Id"], batch_size=100))
        self.assertEqual(len(connector.fetch.call_args.args), 1)


class ShardedCopyBookkeepingTests(SimpleTestCase):
    """Slices are an implementation detail — the report shows one row per table."""

    def test_slice_summaries_merge_into_one_table_result(self):
        from engine.migration.parallel_copy import _merge
        merged = _merge([
            {"rows_copied": 100, "rows_failed": 0, "errors": [], "duration_seconds": 9.0},
            {"rows_copied": 250, "rows_failed": 1, "errors": ["bad row"], "duration_seconds": 4.0},
        ])
        self.assertEqual(merged["rows_copied"], 350)
        self.assertEqual(merged["rows_failed"], 1)
        self.assertEqual(merged["errors"], ["bad row"])
        # Slices run at the same time, so the table took as long as its slowest
        # slice — not the sum, which would report 13s for a 9s table.
        self.assertEqual(merged["duration_seconds"], 9.0)

    def test_a_slice_does_not_manage_the_whole_table(self):
        """Each slice dropping the shared indexes would race the others."""
        table = Table(name="dbo.Big", columns=[Column("Id", "INT")])
        whole = DataMigration(Mock(), Mock(), table)
        slice_ = DataMigration(Mock(), Mock(), table, key_range=("Id", 0, 10),
                               manage_table=False)
        self.assertTrue(whole.manage_table)
        self.assertFalse(slice_.manage_table)

    def test_a_single_worker_never_splits_anything(self):
        from engine.migration.parallel_copy import _build_work
        table = Table(
            name="dbo.Big", columns=[Column("Id", "BIGINT")],
            primary_key=Constraint(name="pk", columns=["Id"]),
        )
        items, plans, _estimates = _build_work(Mock(), [table], workers=1)
        self.assertEqual(plans, {})
        self.assertEqual(items, [(table, 0, None)])


class BalancedSliceBoundaryTests(SimpleTestCase):
    """Equal-width key ranges assume a dense key. Real identity columns are
    gappy, and on the measured run one of sixteen equal-width slices carried
    5.7x the average and became the critical path by itself.
    """

    def test_quantiles_are_preferred_over_equal_width(self):
        from engine.migration.table_shards import _balanced_ranges
        source = Mock()
        source.key_quantiles.return_value = [1, 100, 5000, 900_000]
        ranges = _balanced_ranges(source, "dbo.Big", "Id", 4)
        source.key_bounds.assert_not_called()
        self.assertEqual(ranges, [(None, 100), (100, 5000), (5000, 900_000), (900_000, None)])

    def test_equal_width_is_the_fallback_when_quantiles_fail(self):
        from engine.migration.table_shards import _balanced_ranges
        source = Mock()
        source.key_quantiles.side_effect = RuntimeError("no window functions")
        source.key_bounds.return_value = (1, 4001)
        self.assertGreater(len(_balanced_ranges(source, "dbo.Big", "Id", 4)), 1)

    def test_a_gappy_key_still_balances_by_row_count(self):
        """The real shape: 90% of rows bunched in a narrow band of the key
        range. Equal-width puts them all in one slice; quantiles do not."""
        from engine.migration.table_shards import ranges_from_boundaries, split_range
        keys = sorted(list(range(1, 1000)) + list(range(9_000_000, 9_009_000)))

        def heaviest(ranges):
            counts = [
                sum(1 for k in keys
                    if (lo is None or k >= lo) and (hi is None or k < hi))
                for lo, hi in ranges
            ]
            return max(counts) / (len(keys) / len(ranges))

        even = split_range(keys[0], keys[-1], 8)
        quantiles = ranges_from_boundaries(
            [keys[i * len(keys) // 8] for i in range(1, 8)]
        )
        self.assertGreater(heaviest(even), 3.0)      # equal-width is badly skewed
        self.assertLess(heaviest(quantiles), 1.5)    # quantiles are near-even

    def test_duplicate_boundaries_collapse_instead_of_making_empty_slices(self):
        from engine.migration.table_shards import ranges_from_boundaries
        self.assertEqual(ranges_from_boundaries([7, 7, 7]), [(None, 7), (7, None)])

    def test_boundaries_still_cover_every_key(self):
        from engine.migration.table_shards import ranges_from_boundaries
        ranges = ranges_from_boundaries([10, 20, 30])
        for key in (-5, 10, 15, 30, 10 ** 9):
            hits = sum(
                1 for lo, hi in ranges
                if (lo is None or key >= lo) and (hi is None or key < hi)
            )
            self.assertEqual(hits, 1, key)


class SplitTableFinalizeTests(SimpleTestCase):
    """Index rebuilds for split tables belong in the pool, not the parent."""

    def test_finalize_runs_in_a_worker_and_reports_failure_rather_than_raising(self):
        from engine.migration import parallel_copy
        table = Table(name="dbo.Big", columns=[Column("Id", "INT")])
        broken = Mock()
        broken.recreate_indexes.side_effect = RuntimeError("index rebuild failed")
        with patch.dict(parallel_copy._WORKER,
                        {"source": Mock(), "target": broken}, clear=False):
            name, error = parallel_copy._finalize_one((table, ["CREATE INDEX ..."]))
        self.assertEqual(name, "dbo.Big")
        self.assertIsNone(error)   # data_mover swallows index errors by design

    def test_progress_total_comes_from_the_estimate_not_a_count(self):
        """An exact COUNT(*) meant a full scan of the biggest tables, serially,
        before a single row had been copied."""
        import inspect
        from engine.migration import parallel_copy
        body = inspect.getsource(parallel_copy.copy_tables)
        self.assertNotIn("source.count_rows", body)
        self.assertIn("estimates.get", body)
