"""End-to-end smoke test of the migration pipeline against in-memory connectors.

No live database is required: the fake connector implements the full
DatabaseConnector surface over plain Python dicts, so the orchestrator
(extract -> convert -> DDL -> data copy -> FK/objects -> verify) runs
exactly as it would against real MSSQL/PostgreSQL servers.
"""
from django.test import SimpleTestCase

from engine.connectors.base import ConnectorError
from engine.migration.orchestrator import MigrationOrchestrator
from engine.schema import Column, Constraint, Database, Table


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
