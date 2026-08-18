"""
Build target-dialect DDL from the normalized schema model.

This is the reverse of the extractors: a normalized Database (from MSSQL or
PostgreSQL) becomes a dependency-ordered script of CREATE TABLE / ALTER TABLE
/ CREATE INDEX / CREATE VIEW / CREATE FUNCTION / CREATE PROCEDURE / CREATE
TRIGGER statements in the target dialect, plus standalone sequences where the
target needs them.

Identifier safety notes:
  - PostgreSQL truncates identifiers to 63 bytes; long constraint/index names
    from SQL Server are shortened with a collision-proof suffix.
  - PostgreSQL folds unquoted identifiers to lowercase, so PG output always
    double-quotes identifiers to preserve case.
"""
from __future__ import annotations

import hashlib
import re

from engine.mappers.type_mappings import (
    MSSQL_TO_POSTGRES_TYPE_OVERRIDES,
    MSSQL_TO_PG_COLLATIONS,
    PG_TO_MSSQL_COLLATIONS,
    POSTGRES_TO_MSSQL_TYPE_OVERRIDES,
)
from engine.schema import (
    Column, Constraint, Database, ForeignKey, Index, Permission, Principal, Routine,
    Sequence, Synonym, Table, Trigger, UserType, View,
)

_PG_MAX_IDENT = 63

# T-SQL "synonym" types (no parameters) -> canonical target types.
_MSSQL_TYPES = {
    "INT": "INTEGER", "INTEGER": "INTEGER", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
    "TINYINT": "SMALLINT", "BIT": "BOOLEAN", "MONEY": "NUMERIC(19,4)",
    "SMALLMONEY": "NUMERIC(10,4)", "FLOAT": "DOUBLE PRECISION", "REAL": "REAL",
    "DATE": "DATE", "TIME": "TIME", "DATETIME": "TIMESTAMP",
    "SMALLDATETIME": "TIMESTAMP", "DATETIMEOFFSET": "TIMESTAMPTZ",
    "DATETIME2": "TIMESTAMP", "TEXT": "TEXT", "NTEXT": "TEXT",
    "IMAGE": "BYTEA", "BINARY": "BYTEA", "VARBINARY": "BYTEA",
    "UNIQUEIDENTIFIER": "UUID", "SYSNAME": "VARCHAR(128)",
    "XML": "XML", "SQL_VARIANT": None, "GEOGRAPHY": None, "GEOMETRY": None,
    "HIERARCHYID": None, "ROWVERSION": None, "TIMESTAMP": None,
}

# PostgreSQL type patterns -> T-SQL. Keys are regexes matched against the
# lowercased type string.
_PG_TYPES = [
    # order matters: more specific first
    (r"^bigserial$", "BIGINT IDENTITY(1,1)"),
    (r"^smallserial$", "SMALLINT IDENTITY(1,1)"),
    (r"^serial$", "INT IDENTITY(1,1)"),
    (r"^integer$", "INT"),
    (r"^int$", "INT"),
    (r"^bigint$", "BIGINT"),
    (r"^smallint$", "SMALLINT"),
    (r"^boolean$", "BIT"),
    (r"^numeric$", "NUMERIC"),
    (r"^decimal$", "NUMERIC"),
    (r"^numeric\(([\d,]+)\)$", r"NUMERIC(\1)"),
    (r"^decimal\(([\d,]+)\)$", r"NUMERIC(\1)"),
    (r"^double precision$", "FLOAT"),
    (r"^real$", "REAL"),
    (r"^money$", "MONEY"),
    (r"^character varying\((\d+)\)$", r"NVARCHAR(\1)"),
    (r"^character varying$", "NVARCHAR(MAX)"),
    (r"^character\((\d+)\)$", r"NCHAR(\1)"),
    (r"^varchar\((\d+)\)$", r"NVARCHAR(\1)"),
    (r"^varchar$", "NVARCHAR(MAX)"),
    (r"^char\((\d+)\)$", r"NCHAR(\1)"),
    (r"^text$", "NVARCHAR(MAX)"),
    (r"^bytea$", "VARBINARY(MAX)"),
    (r"^uuid$", "UNIQUEIDENTIFIER"),
    (r"^date$", "DATE"),
    (r"^time( without time zone)?(\(\d+\))?$", r"TIME\2"),
    (r"^timetz(\(\d+\))?$", r"TIME\1"),
    (r"^timestamp(\(\d+\))? without time zone$", r"DATETIME2\1"),
    (r"^timestamp(\(\d+\))? with time zone$", r"DATETIMEOFFSET\1"),
    (r"^timestamp without time zone(\(\d+\))?$", r"DATETIME2\1"),
    (r"^timestamp with time zone(\(\d+\))?$", r"DATETIMEOFFSET\1"),
    (r"^timestamp(\(\d+\))?$", r"DATETIME2\1"),
    (r"^jsonb?$", "NVARCHAR(MAX)"),
    (r"^xml$", "XML"),
]

# PG types with no clean MSSQL equivalent.
_PG_REVIEW_TYPES = ("array", "hstore", "int4range", "int8range", "numrange",
                    "tsrange", "tstzrange", "daterange", "inet", "cidr", "macaddr", "point")


def convert_type(data_type: str, source_dialect: str, target_dialect: str) -> tuple[str | None, str | None]:
    """Map a source-native type to a target type.

    Returns (target_type, warning). target_type is None for unmappable types
    (caller must skip the object and record the warning).
    """
    if source_dialect == "tsql" and target_dialect == "postgres":
        base, params = _split_type(data_type)
        base_upper = base.upper()
        is_max = params is not None and params.strip().upper() == "MAX"
        mapped = _MSSQL_TYPES.get(base_upper)
        if mapped is None:
            if base_upper in MSSQL_TO_POSTGRES_TYPE_OVERRIDES:
                mapped = MSSQL_TO_POSTGRES_TYPE_OVERRIDES[base_upper]
        if mapped is None:
            if base_upper in ("DECIMAL", "NUMERIC"):
                mapped = f"NUMERIC({params})" if params else "NUMERIC"
            elif base_upper in ("VARCHAR", "CHAR", "NVARCHAR", "NCHAR"):
                # PostgreSQL has no N-prefixed types — VARCHAR/CHAR hold unicode.
                # VARCHAR(MAX)/NVARCHAR(MAX) become TEXT (no length limit in PG).
                plain = base_upper[1:] if base_upper.startswith("N") else base_upper
                mapped = "TEXT" if is_max else f"{plain}({params})" if params else "TEXT"
            else:
                return None, f"Type '{base_upper}' has no PostgreSQL equivalent — manual review required"
        if base_upper in ("GEOGRAPHY", "GEOMETRY", "HIERARCHYID", "SQL_VARIANT", "ROWVERSION"):
            return mapped, f"Type '{base_upper}' has no clean PostgreSQL equivalent — verify converted output"
        return mapped, None

    if source_dialect == "postgres" and target_dialect == "tsql":
        lowered = data_type.strip().lower()
        for pattern, replacement in _PG_TYPES:
            m = re.match(pattern, lowered)
            if m:
                return m.expand(replacement), None
        for review in _PG_REVIEW_TYPES:
            if review in lowered:
                return None, f"PostgreSQL type '{lowered}' has no clean SQL Server equivalent — manual review required"
        return None, f"Type '{lowered}' has no SQL Server equivalent — manual review required"

    return data_type, None


def _split_type(data_type: str) -> tuple[str, str | None]:
    cleaned = re.sub(r"[\[\]]", "", data_type.strip())
    match = re.match(r"^(\w+)(?:\(([^)]*)\))?$", cleaned)
    if not match:
        return cleaned, None
    return match.group(1), match.group(2)


def build_database_ddl(database: Database, target_dialect: str) -> tuple[list[str], list[str]]:
    """Return (statements, warnings) for the target dialect, dependency-ordered."""
    statements: list[str] = []
    warnings: list[str] = []
    source = database.dialect

    # User-defined types and standalone sequences must exist before any table
    # references them.
    for ut in database.types:
        stmts, tw = build_type_ddl(ut, target_dialect, source)
        statements.extend(stmts)
        warnings.extend(tw)

    for seq in database.sequences:
        stmts, tw = build_sequence_ddl(seq, target_dialect, source)
        statements.extend(stmts)
        warnings.extend(tw)

    for table in database.all_tables_in_dependency_order():
        stmts, tw = build_table_ddl(table, target_dialect, source, database)
        statements.extend(stmts)
        warnings.extend(tw)

    # Constraints/indexes/FKs added after all tables exist so FK targets exist.
    for table in database.all_tables_in_dependency_order():
        for uc in table.unique_constraints:
            stmts, tw = build_unique_constraint_ddl(table, uc, target_dialect)
            statements.extend(stmts)
            warnings.extend(tw)
        for idx in table.indexes:
            stmts, tw = build_index_ddl(table, idx, target_dialect, source)
            statements.extend(stmts)
            warnings.extend(tw)
        for fk in table.foreign_keys:
            stmts, tw = build_foreign_key_ddl(table, fk, target_dialect)
            statements.extend(stmts)
            warnings.extend(tw)
        for check in table.check_constraints:
            statements.append(build_check_ddl(table, check, target_dialect))
    for view in database.views:
        stmts, tw = build_view_ddl(view, target_dialect, source, database.tables)
        statements.extend(stmts)
        warnings.extend(tw)

    for fn in database.functions:
        stmts, tw = build_function_ddl(fn, target_dialect, database.tables, database.types)
        statements.extend(stmts)
        warnings.extend(tw)

    for proc in database.procedures:
        stmts, tw = build_procedure_ddl(proc, target_dialect, database.tables, database.types)
        statements.extend(stmts)
        warnings.extend(tw)

    for trig in database.triggers:
        stmts, tw = build_trigger_ddl(trig, target_dialect)
        statements.extend(stmts)
        warnings.extend(tw)

    # Synonyms wrap tables/views and must come after they exist.
    for syn in database.synonyms:
        stmts, tw = build_synonym_ddl(syn, target_dialect)
        statements.extend(stmts)
        warnings.extend(tw)

    stmts, tw = build_security_ddl(database, target_dialect)
    statements.extend(stmts)
    warnings.extend(tw)

    table_names = [t.name for t in database.tables]
    statements = [_qualify_body_refs(s, database.tables, target_dialect) for s in statements]

    return statements, warnings


def build_table_ddl(table: Table, target_dialect: str, source_dialect: str,
                    database: Database | None = None,
                    downgrade_computed: bool = False) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    col_lines = []
    for col in table.columns:
        line, warn = _build_column(col, target_dialect, source_dialect, database,
                                   downgrade_computed=downgrade_computed)
        if line is None:
            warnings.append(f"Table '{table.name}' column '{col.name}': {warn}")
            continue
        col_lines.append(line)
        if warn:
            warnings.append(f"Table '{table.name}' column '{col.name}': {warn}")

    if not col_lines:
        return [], [f"Table '{table.name}' had no convertible columns and was skipped"]

    pk_line = ""
    if table.primary_key:
        pk_line = f",\n  CONSTRAINT {_qident(table.primary_key.name, target_dialect)} PRIMARY KEY ({_qcols(table.primary_key.columns, target_dialect)})"
        if table.is_partitioned and target_dialect == "postgres" and table.partition_key:
            key = _split_type(table.partition_key)[0].strip('"[]')
            if key.lower() not in {c.lower() for c in table.primary_key.columns}:
                warnings.append(
                    f"Partitioned table '{table.name}': PostgreSQL requires the partition key "
                    f"'{table.partition_key}' to be part of the primary key — the DDL below will "
                    f"fail until the key is added"
                )

    col_separator = ",\n  "
    create = (
        f"CREATE TABLE {_qident(table.name, target_dialect)} (\n"
        f"  {col_separator.join(col_lines)}{pk_line}\n)"
    )

    statements = [create]

    if table.is_partitioned:
        if target_dialect == "postgres" and table.partition_key and table.partition_type in ("range", "list"):
            create += f"\nPARTITION BY {table.partition_type.upper()} ({_translate_expr(table.partition_key, source_dialect, target_dialect)})"
            statements[0] = create
            for child in table.partition_children:
                statements.append(
                    f"CREATE TABLE {_qident(child.name, target_dialect)} PARTITION OF "
                    f"{_qident(table.name, target_dialect)} FOR VALUES {child.bounds}"
                )
        else:
            warnings.append(
                f"Table '{table.name}' is partitioned but the target dialect cannot recreate its "
                f"partitioning — the table was created as a regular table"
            )

    return statements, warnings


def _build_column(col: Column, target: str, source: str,
                  database: Database | None = None,
                  downgrade_computed: bool = False) -> tuple[str | None, str | None]:
    target_type, warn = _resolve_column_type(col.data_type, source, target, database)
    if target_type is None:
        return None, warn

    collation_frag, collation_warn = _append_collation(
        col.collation, target_type, target, database
    )
    if collation_warn:
        warn = collation_warn if warn is None else f"{warn}; {collation_warn}"

    parts = [f"{_qident(col.name, target)} {target_type}"]

    if col.is_computed and not downgrade_computed:
        # T-SQL computed columns must NOT declare a type — it is inferred
        # from the expression. A PG-generated column's default_expr IS its
        # generation expression; never emit computed columns as DEFAULT
        # (SQL Server rejects column references in DEFAULT).
        expr = _translate_expr(col.computed_definition or col.default, source, target)
        if target == "postgres":
            col_type = f" {target_type}" if target_type else ""
            parts = [f"{_qident(col.name, target)}{col_type}"]
            parts[0] += f" GENERATED ALWAYS AS ({expr}) STORED"
        else:
            parts = [f"{_qident(col.name, target)}"]
            parts[0] += f" AS ({expr}) PERSISTED"
    else:
        # COLLATE must directly follow the data type in both dialects — never
        # after IDENTITY/DEFAULT/NOT NULL (T-SQL error 156).
        parts[0] += collation_frag

        identity = ""
        if col.is_identity:
            seed = col.identity_seed if col.identity_seed is not None else 1
            inc = col.identity_increment if col.identity_increment is not None else 1
            if target == "postgres":
                identity = f" GENERATED BY DEFAULT AS IDENTITY (START WITH {seed} INCREMENT BY {inc})"
            else:
                identity = f" IDENTITY({seed},{inc})"

        parts[0] += identity

        # IDENTITY and DEFAULT are mutually exclusive in both dialects. A PG
        # serial column carries a nextval() default that must not be emitted
        # next to the IDENTITY clause (T-SQL error 195).
        if not col.is_identity:
            default = _translate_default(col.default, target)
            if default:
                if target == "postgres" and target_type.upper() == "BOOLEAN" and default in ("1", "0"):
                    default = "true" if default == "1" else "false"
                parts[0] += f" DEFAULT {default}"

        if not col.nullable:
            # Downgraded computed columns must be nullable: the source data
            # excludes computed columns, so target rows will have NULLs.
            if not (col.is_computed and downgrade_computed):
                parts[0] += " NOT NULL"

    return parts[0], warn


def _resolve_column_type(data_type: str, source: str, target: str,
                         database: Database | None) -> tuple[str | None, str | None]:
    """Map a column type, resolving user-defined types against the schema model."""
    base, params = _split_type(data_type)
    ut = database.type_by_name(base) if database else None

    if ut is not None and source == "tsql" and target == "postgres" and ut.kind == "alias":
        # The alias becomes a DOMAIN in PostgreSQL; the column references it.
        return _qident(ut.name, "postgres"), None

    if ut is not None and source == "postgres" and target == "tsql":
        if ut.kind == "domain":
            # The domain is recreated as a SQL Server alias type; the column
            # references it by name so the domain is actually used.
            return _qident(ut.name, "tsql"), None
        if ut.kind == "enum":
            return "NVARCHAR(MAX)", (
                f"PostgreSQL enum type '{ut.name}' has no SQL Server equivalent — "
                f"column mapped to NVARCHAR(MAX)"
            )
        if ut.kind == "composite":
            return "NVARCHAR(MAX)", (
                f"PostgreSQL composite type '{ut.name}' has no SQL Server equivalent — "
                f"column mapped to NVARCHAR(MAX)"
            )

    return convert_type(data_type, source, target)


def _append_collation(collation: str | None, target_type: str,
                      target: str, database: Database | None) -> tuple[str, str | None]:
    """Build a COLLATE clause for character columns where a mapping is known.

    Returns ``(clause, warning)``. The clause is empty when the column inherits
    the source database's default collation or no mapping exists; the warning
    explains why the clause was omitted.
    """
    if not collation or not _is_character_type(target_type):
        return "", None
    if database and database.collation and collation.lower() == database.collation.lower():
        # Column uses the source database's default collation, which the target
        # database will apply anyway — no need to pin it per column.
        return "", None
    key = collation.strip().lower()
    if target == "postgres":
        mapped = MSSQL_TO_PG_COLLATIONS.get(key)
        if not mapped:
            return "", (f"Collation '{collation}' has no PostgreSQL equivalent — column created with the database collation")
        return f' COLLATE "{mapped}"', None
    mapped = PG_TO_MSSQL_COLLATIONS.get(key)
    if not mapped:
        return "", (f"Collation '{collation}' has no SQL Server equivalent — column created with the database collation")
    return f" COLLATE {mapped}", None


def _is_character_type(target_type: str) -> bool:
    upper = target_type.upper()
    return any(token in upper for token in ("VARCHAR", "CHAR", "TEXT", "SYSNAME"))


def build_unique_constraint_ddl(table: Table, uc: Constraint, target: str) -> tuple[list[str], list[str]]:
    stmt = (
        f"ALTER TABLE {_qident(table.name, target)} ADD CONSTRAINT "
        f"{_qident(uc.name, target)} UNIQUE ({_qcols(uc.columns, target)})"
    )
    return [stmt], []


def build_index_ddl(table: Table, index: Index, target: str, source: str) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    if index.index_type:
        warnings.append(
            f"Index '{index.name}' on '{table.name}' is a {index.index_type} index with no "
            f"{'PostgreSQL' if target == 'postgres' else 'SQL Server'} equivalent and was not migrated"
        )
        return [], warnings
    unique = "UNIQUE " if index.unique else ""
    clustered = ""
    if index.is_clustered:
        if target == "tsql":
            clustered = "CLUSTERED "
        else:
            warnings.append(
                f"Clustered index '{index.name}' on '{table.name}': PostgreSQL does not enforce "
                f"clustered storage — the index was created as a regular index"
            )
    stmt = f"CREATE {unique}{clustered}INDEX {_qident(index.name, target)} ON {_qident(table.name, target)} ({_qcols(index.columns, target)})"
    if index.included_columns:
        stmt += f" INCLUDE ({_qcols(index.included_columns, target)})"
    if index.where and target == "tsql":
        where = _translate_expr(index.where, source, target)
        stmt += f" WHERE {where}"
    elif index.where:
        # PostgreSQL keeps filtered indexes as partial indexes.
        where = _translate_expr(index.where, source, target)
        where = _normalize_index_where_columns(where, table)
        where = fix_boolean_predicates(where, [table], source)
        stmt += f" WHERE {where}"
    return [stmt], warnings


def build_foreign_key_ddl(table: Table, fk: ForeignKey, target: str) -> tuple[list[str], list[str]]:
    stmt = (
        f"ALTER TABLE {_qident(table.name, target)} ADD CONSTRAINT {_qident(fk.name, target)} "
        f"FOREIGN KEY ({_qcols(fk.columns, target)}) "
        f"REFERENCES {_qident(fk.ref_table, target)} ({_qcols(fk.ref_columns, target)})"
    )
    if fk.on_delete.upper() not in ("NO ACTION",):
        stmt += f" ON DELETE {fk.on_delete}"
    if fk.on_update.upper() not in ("NO ACTION",):
        stmt += f" ON UPDATE {fk.on_update}"
    return [stmt], []


def build_check_ddl(table: Table, check, target: str) -> str:
    name = check.name or _anon_name(table, check.definition)
    if target == "postgres":
        # MSSQL sys.check_constraints.definition looks like "([Status]=N'Pending')"
        expr = _translate_expr(check.definition.strip(), "tsql", "postgres")
        if not expr.upper().startswith("CHECK"):
            expr = f"CHECK {expr}"
    else:
        # pg_get_constraintdef returns "CHECK ((Price >= 0))" — normalize identifiers back
        expr = _translate_expr(check.definition.strip(), "postgres", "tsql")
    return f"ALTER TABLE {_qident(table.name, target)} ADD CONSTRAINT {_qident(pg_ident(name), target)} {expr}"


def build_view_ddl(view: View, target: str, source: str, tables: list | None = None) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    definition = (view.definition or "").strip()
    m = re.search(r"\bAS\s+(SELECT\b.*)$", definition, re.IGNORECASE | re.DOTALL)
    if m:
        definition = m.group(1)
    definition = _translate_expr(definition, source, target)
    if source == "tsql" and target == "postgres":
        definition = fix_boolean_predicates(definition, tables or [], "tsql")

    name = _qident(view.name, target)

    if view.is_materialized:
        if target == "postgres":
            stmts = [f"CREATE MATERIALIZED VIEW {name} AS {definition}"]
            for idx in view.indexes:
                if idx.index_type:
                    warnings.append(
                        f"Index '{idx.name}' on materialized view '{view.name}' is a {idx.index_type} "
                        f"index with no PostgreSQL equivalent and was not migrated"
                    )
                    continue
                uniq = "UNIQUE " if idx.unique else ""
                stmt = f"CREATE {uniq}INDEX {_qident(idx.name, target)} ON {name} ({_qcols(idx.columns, target)})"
                if idx.included_columns:
                    stmt += f" INCLUDE ({_qcols(idx.included_columns, target)})"
                stmts.append(stmt)
            # The indexes must be created right after the matview, before any
            # data copy phase could re-order them — bundle into one statement.
            return ["\n".join(stmts)], warnings

        # SQL Server indexed views need SCHEMABINDING and a unique clustered
        # index to materialize — the closest structural equivalent.
        stmts = [f"CREATE VIEW {name} WITH SCHEMABINDING AS {definition}"]
        if view.indexes:
            clustered_seen = False
            for idx in view.indexes:
                uniq = "UNIQUE " if idx.unique else ""
                clustered = "CLUSTERED " if idx.is_clustered else ""
                stmt = (
                    f"CREATE {uniq}{clustered}INDEX {_qident(idx.name, target)} ON {name} "
                    f"({_qcols(idx.columns, target)})"
                )
                if idx.included_columns:
                    stmt += f" INCLUDE ({_qcols(idx.included_columns, target)})"
                stmts.append(stmt)
                clustered_seen = clustered_seen or idx.is_clustered
            if not clustered_seen:
                warnings.append(
                    f"Indexed view '{view.name}' has no clustered index — SQL Server requires a "
                    f"unique clustered index for indexed views; the view is only created with "
                    f"SCHEMABINDING"
                )
        else:
            warnings.append(
                f"Materialized view '{view.name}' has no indexes — SQL Server indexed views "
                f"require a unique clustered index; the view is only created with SCHEMABINDING"
            )
        return ["\n".join(stmts)], warnings

    prefix = "CREATE OR REPLACE VIEW" if target == "postgres" else "CREATE OR ALTER VIEW"
    stmt = f"{prefix} {name} AS {definition}"
    return [stmt], []


def build_function_ddl(fn: Routine, target: str, tables: list | None = None,
                       user_types: list | None = None) -> tuple[list[str], list[str]]:
    from engine.translators.procedure_translator import translate_routine
    converted, warnings = translate_routine(fn.definition, source=f"{fn.kind}", target=target,
                                            tables=tables, user_types=user_types)
    if converted:
        return [converted], warnings
    return [], [f"Function '{fn.name}' could not be converted: {warnings}"]


def build_procedure_ddl(proc: Routine, target: str, tables: list | None = None,
                        user_types: list | None = None) -> tuple[list[str], list[str]]:
    from engine.translators.procedure_translator import translate_routine
    converted, warnings = translate_routine(proc.definition, source="procedure", target=target,
                                            tables=tables, user_types=user_types)
    if converted:
        return [converted], warnings
    return [], [f"Procedure '{proc.name}' could not be converted: {warnings}"]


def build_trigger_ddl(trigger: Trigger, target: str) -> tuple[list[str], list[str]]:
    if trigger.is_ddl:
        from engine.translators.ddl_trigger_translator import translate_ddl_trigger
        converted, warnings = translate_ddl_trigger(trigger, target)
        if converted:
            return [converted], warnings
        return [], [f"DDL trigger '{trigger.name}' could not be converted: {warnings}"]
    from engine.translators.trigger_translator import translate_trigger
    converted, warnings = translate_trigger(trigger, target)
    if converted:
        return [converted], warnings
    return [], [f"Trigger '{trigger.name}' could not be converted: {warnings}"]


def build_type_ddl(ut: UserType, target: str, source: str) -> tuple[list[str], list[str]]:
    """Convert a user-defined type into target-dialect DDL.

    MSSQL alias types become PostgreSQL DOMAINs and MSSQL table types become
    PostgreSQL composite types (TVP parameters use composite arrays).
    PostgreSQL DOMAINs become MSSQL alias types. Other composite/enum/CLR
    conversions without a safe equivalent are surfaced as warnings.
    """
    if source == "tsql" and target == "postgres":
        if ut.kind == "table_type":
            if not ut.columns:
                return [], [f"Table type '{ut.name}' has no columns and could not be converted"]
            attributes = []
            warnings = []
            for column in ut.columns:
                mapped, warn = convert_type(column.data_type, "tsql", "postgres")
                if not mapped:
                    return [], [f"Table type '{ut.name}' column '{column.name}' could not be mapped: {warn}"]
                attributes.append(f"{_qident(column.name, 'postgres')} {mapped}")
                if warn:
                    warnings.append(f"Table type '{ut.name}' column '{column.name}': {warn}")
            stmt = f"CREATE TYPE {_qident(ut.name, 'postgres')} AS ({', '.join(attributes)})"
            warnings.append(
                f"Table type '{ut.name}' was converted to a PostgreSQL composite type; "
                "table-valued parameters use an array of this type"
            )
            return [stmt], warnings
        if ut.kind != "alias":
            return [], []
        base, warn = convert_type(ut.base_type or "", "tsql", "postgres")
        if base is None:
            return [], [f"User-defined type '{ut.name}' (alias for '{ut.base_type}') could not be mapped: {warn}"]
        stmt = f"CREATE DOMAIN {_qident(ut.name, target)} AS {base}"
        if ut.default:
            stmt += f" DEFAULT {_translate_default(ut.default, target)}"
        if not ut.nullable:
            stmt += " NOT NULL"
        warnings = []
        if warn:
            warnings.append(f"User-defined type '{ut.name}': {warn}")
        return [stmt], warnings

    if source == "postgres" and target == "tsql":
        if ut.kind == "domain":
            base, warn = convert_type(ut.base_type or "", "postgres", "tsql")
            if base is None:
                return [], [f"Domain '{ut.name}' base type '{ut.base_type}' could not be mapped: {warn}"]
            stmt = f"CREATE TYPE {_qident(ut.name, target)} FROM {base}"
            if not ut.nullable:
                stmt += " NOT NULL"
            if ut.default:
                stmt += f" DEFAULT {_translate_default(ut.default, target)}"
            warnings = []
            if warn:
                warnings.append(f"Domain '{ut.name}': {warn}")
            if ut.constraints:
                warnings.append(
                    f"Domain '{ut.name}' has {len(ut.constraints)} CHECK constraint(s) that SQL "
                    f"Server alias types cannot express — the constraints were not migrated"
                )
            return [stmt], warnings
        if ut.kind == "enum":
            return [], [f"PostgreSQL enum type '{ut.name}' has no SQL Server equivalent and was not migrated"]
        if ut.kind == "composite":
            if not ut.columns:
                return [], [f"PostgreSQL composite type '{ut.name}' has no attributes and was not migrated"]
            columns = []
            warnings = []
            for column in ut.columns:
                mapped, warn = convert_type(column.data_type, "postgres", "tsql")
                if not mapped:
                    return [], [f"Composite type '{ut.name}' attribute '{column.name}' could not be mapped: {warn}"]
                nullability = " NULL" if column.nullable else " NOT NULL"
                columns.append(f"{_qident(column.name, 'tsql')} {mapped}{nullability}")
                if warn:
                    warnings.append(f"Composite type '{ut.name}' attribute '{column.name}': {warn}")
            warnings.append(
                f"PostgreSQL composite type '{ut.name}' was converted to a SQL Server table type"
            )
            return [f"CREATE TYPE {_qident(ut.name, 'tsql')} AS TABLE ({', '.join(columns)})"], warnings
    return [], []


def build_sequence_ddl(seq: Sequence, target: str, source: str) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    type_clause = ""
    if seq.data_type:
        mapped, warn = convert_type(seq.data_type, source, target)
        if mapped and warn is None:
            type_clause = f" AS {mapped}"
        elif mapped:
            # keep the mapped type but surface the review warning
            type_clause = f" AS {mapped}"
            warnings.append(f"Sequence '{seq.name}': {warn}")
        else:
            warnings.append(f"Sequence '{seq.name}': {warn}")
    start = seq.start_value
    if target == "postgres" and start < 1:
        start = 1
    stmt = (
        f"CREATE SEQUENCE {_qident(seq.name, target)}"
        f"{type_clause} START WITH {start} INCREMENT BY {seq.increment}"
    )
    if seq.is_cycling:
        stmt += " CYCLE"
    statements = [stmt]
    if seq.current_value is not None and seq.current_value != seq.start_value:
        if target == "postgres":
            statements.append(
                f"SELECT setval({_qstr(seq.name)}, {seq.current_value}, true)"
            )
        else:
            statements.append(
                f"ALTER SEQUENCE {_qident(seq.name, target)} RESTART WITH {seq.current_value}"
            )
    return statements, warnings


def build_synonym_ddl(syn: Synonym, target: str) -> tuple[list[str], list[str]]:
    """PostgreSQL has no synonyms; a table/view synonym is recreated as a view
    wrapper that SELECTs from the target. Procedure/function synonyms cannot be
    recreated and are surfaced as warnings. The reverse direction emits a
    warning instead of SQL: such wrappers were synonyms on the original SQL
    Server database and are assumed to already exist on the target."""
    if target == "tsql":
        return [], [
            f"Synonym '{syn.name}' was materialized as a view during the SQL Server -> "
            f"PostgreSQL migration; it is not recreated as a view (the target should "
            f"already define the original synonym '{syn.target_object}')"
        ]
    if target != "postgres":
        return [], []
    if syn.target_kind not in ("table", "view"):
        return [], [
            f"Synonym '{syn.name}' targets a {syn.target_kind} — PostgreSQL has no synonym "
            f"support for it; create an equivalent wrapper manually"
        ]
    stmt = (
        f"CREATE VIEW {_qident(syn.name, 'postgres')} AS "
        f"SELECT * FROM {_qident(_normalize_object_ref(syn.target_object), 'postgres')}"
    )
    return [stmt], []


def _normalize_object_ref(name: str) -> str:
    """Normalize an object reference read from SQL Server catalog text.

    ``sys.synonyms.base_object_name`` returns the target exactly as it was
    authored, so it may carry bracket delimiters (``[dbo].[Inventory]``) and an
    optional server/database prefix (``[srv].[db].[dbo].[Inventory]``). Strip
    the delimiters and drop the leading server/database parts so the remaining
    ``schema.object`` can be quoted correctly for PostgreSQL."""
    parts = [p.strip().strip("[]").strip() for p in (name or "").split(".")]
    parts = [p for p in parts if p]
    while len(parts) > 2:
        parts.pop(0)
    return ".".join(parts)


def build_security_ddl(database: Database, target: str) -> tuple[list[str], list[str]]:
    """Recreate principals (users/roles), role memberships and GRANTs."""
    statements: list[str] = []
    warnings: list[str] = []
    principals = database.roles + database.users

    # 'public' exists implicitly in both engines and its name is reserved in
    # PostgreSQL; a leftover from manual schema construction must not emit a
    # failing CREATE ROLE.
    if target == "postgres":
        principals = [p for p in principals if p.name.lower() != "public"]
        for skipped in [p for p in database.roles + database.users if p.name.lower() == "public"]:
            warnings.append(
                f"Principal '{skipped.name}' is reserved in PostgreSQL and was not recreated"
            )

    if target == "postgres":
        for role in [p for p in principals if p.kind == "role"]:
            statements.append(
                f"DO $$ BEGIN CREATE ROLE {_qident(role.name, 'postgres')}; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        for user in [p for p in principals if p.kind == "user"]:
            statements.append(
                f"DO $$ BEGIN CREATE ROLE {_qident(user.name, 'postgres')} LOGIN; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
    else:
        for role in database.roles:
            statements.append(f"CREATE ROLE {_qident(role.name, 'tsql')}")
        for user in database.users:
            statements.append(f"CREATE USER {_qident(user.name, 'tsql')} WITHOUT LOGIN")

    for principal in principals:
        for parent in principal.member_of:
            if target == "postgres":
                statements.append(
                    f"GRANT {_qident(parent, 'postgres')} TO {_qident(principal.name, 'postgres')}"
                )
            else:
                statements.append(
                    f"ALTER ROLE {_qident(parent, 'tsql')} ADD MEMBER {_qident(principal.name, 'tsql')}"
                )

    for perm in database.permissions:
        stmt = _build_grant(perm, target)
        if stmt:
            statements.append(stmt)
        else:
            warnings.append(
                f"{perm.grant_type} permission '{perm.action}' on '{perm.object_name}' for "
                f"'{perm.principal}' cannot be ported to {'PostgreSQL' if target == 'postgres' else 'SQL Server'} and was not migrated"
            )
    return statements, warnings


def _build_grant(perm: Permission, target: str) -> str | None:
    if perm.grant_type != "GRANT":
        return None
    principal = _qident(perm.principal, target)
    with_grant = " WITH GRANT OPTION" if perm.with_grant else ""

    if perm.securable == "schema":
        if target == "postgres":
            return f"GRANT USAGE ON SCHEMA {_qident(perm.object_name, target)} TO {principal}{with_grant}"
        return f"GRANT {perm.action} ON SCHEMA::{_qident(perm.object_name, target)} TO {principal}{with_grant}"

    if perm.securable in ("table", "view", "procedure", "function"):
        if target == "postgres":
            object_ref = _qident(perm.object_name, target)
            if perm.securable in ("procedure", "function"):
                # PostgreSQL identifies routines by name plus argument types,
                # while SQL Server grants are name-based. Resolve all emitted
                # overloads at apply time. If conversion skipped a routine,
                # this safely grants nothing instead of raising another error.
                parts = perm.object_name.split(".", 1)
                schema = parts[0] if len(parts) == 2 else "public"
                routine = parts[-1]
                grant_option = " WITH GRANT OPTION" if perm.with_grant else ""
                return (
                    "DO $$ DECLARE r record; BEGIN FOR r IN "
                    "SELECT p.oid::regprocedure::text AS signature FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    f"WHERE n.nspname = {_sql_string(schema)} "
                    f"AND p.proname = {_sql_string(routine)} LOOP "
                    f"EXECUTE format('GRANT EXECUTE ON ROUTINE %s TO %I{grant_option}', "
                    f"r.signature, {_sql_string(perm.principal)}); END LOOP; END $$"
                )
        else:
            object_ref = f"OBJECT::{_qident(perm.object_name, target)}"
        return f"GRANT {perm.action} ON {object_ref} TO {principal}{with_grant}"

    return None


# ---------------------------------------------------------------------------
# expression / default translation (best-effort regex)
# ---------------------------------------------------------------------------

def _translate_default(expr: str | None, target: str) -> str | None:
    if not expr:
        return None
    expr = expr.strip()
    if target == "postgres":
        # Defaults come straight from OBJECT_DEFINITION and can contain any
        # T-SQL expression (CONVERT([nvarchar], GETDATE(), 100), '+'
        # concatenation, ...). Run the full expression translator instead of a
        # few regexes so nothing invalid survives into the emitted DDL.
        from engine.translators.functions import translate_functions
        expr = translate_functions(expr, "tsql", "postgres")
        expr = re.sub(r"\[([\w\s\d_]+)\]", r'"\1"', expr)
        expr = re.sub(r'"\[([\w\s\d_]+)\]"', r'"\1"', expr)
        expr = re.sub(r"\bN'", "'", expr, flags=re.IGNORECASE)
        expr = _translate_top(expr)
        expr = re.sub(
            r"\bNEXT\s+VALUE\s+FOR\s+((?:\"[^\"]+\"|[\w]+)(?:\.(?:\"[^\"]+\"|[\w]+))?)",
            lambda m: f"nextval({_qstr(m.group(1).replace(chr(34), ''))}::regclass)",
            expr,
            flags=re.IGNORECASE,
        )
        # Without grouping, PostgreSQL treats AT TIME ZONE as DDL syntax
        # following the DEFAULT rather than as part of the expression.
        if re.search(r"\bAT\s+TIME\s+ZONE\b", expr, re.IGNORECASE):
            expr = f"({expr})"
        if "'" in expr:
            from engine.translators.procedure_translator import _replace_concat
            expr = _replace_concat(expr)
    else:
        # SQL Server rejects a DEFAULT written as `(NEXT VALUE FOR ...)` with a
        # syntax error at '(' — emit the bare form. Handle both the bare and the
        # parenthesized forms pg_get_expr can produce.
        expr = re.sub(
            r"\(\s*nextval\(\s*'([^']+)'\s*::regclass\s*\)\s*\)",
            lambda m: f"NEXT VALUE FOR {_qident(m.group(1).replace(chr(34), ''), 'mssql')}",
            expr,
            flags=re.IGNORECASE,
        )
        expr = re.sub(
            r"\bnextval\(\s*'([^']+)'\s*::regclass\s*\)",
            lambda m: f"NEXT VALUE FOR {_qident(m.group(1).replace(chr(34), ''), 'mssql')}",
            expr,
            flags=re.IGNORECASE,
        )
        expr = re.sub(r"\bCURRENT_TIMESTAMP\b", "GETDATE()", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bgen_random_uuid\(\)", "NEWID()", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bnow\(\)", "GETDATE()", expr, flags=re.IGNORECASE)
        # PG casts come out of pg_get_expr (e.g. 'Pending'::character varying,
        # (nextval(...))::character varying(10)). Strip them fully including
        # any type modifiers, otherwise the leftover parentheses break the DDL.
        expr = _strip_pg_casts(expr)
        # true/false -> 1/0, including parenthesized forms like `(false)`
        expr = _translate_pg_boolean_literals(expr)
    return expr


def _translate_expr(expr: str, source: str, target: str) -> str:
    from engine.translators.functions import translate_functions
    out = translate_functions(expr, source, target)
    if target == "postgres":
        # MSSQL bracket identifiers -> PG quoted identifiers
        out = re.sub(r"\[([\w\s\d_]+)\]", r'"\1"', out)
        # Heal identifiers whose content was already bracketed (e.g. from
        # catalog text quoted once more) so no '[' survives into PG DDL.
        out = re.sub(r'"\[([\w\s\d_]+)\]"', r'"\1"', out)
        # MSSQL N-prefixed string literals have no PG equivalent
        out = re.sub(r"\bN'", "'", out, flags=re.IGNORECASE)
        # MSSQL '+' string concatenation -> PostgreSQL '||'
        if "'" in out:
            from engine.translators.procedure_translator import _replace_concat
            out = _replace_concat(out)
        out = _translate_top(out)
    else:
        # PG quoted identifiers -> MSSQL brackets
        out = re.sub(r'"([\w\s\d_]+)"', r"[\1]", out)
        out = _strip_pg_casts(out)
        out = _translate_pg_boolean_literals(out)
    return out


def _strip_pg_casts(text: str) -> str:
    """Remove PostgreSQL ``::type`` suffixes without consuming SQL keywords.

    Handles multi-word and parameterized types such as ``::character varying``
    and ``::numeric(12,2)``.
    """
    pg_type = (
        r"(?:double\s+precision|character\s+varying|timestamp(?:\(\d+\))?\s+"
        r"(?:with|without)\s+time\s+zone|time(?:\(\d+\))?\s+"
        r"(?:with|without)\s+time\s+zone|[A-Za-z_][\w.]*)"
        r"(?:\s*\([^)]*\))?(?:\[\])?"
    )
    return re.sub(rf"\s*::\s*{pg_type}", "", text, flags=re.IGNORECASE)


def _translate_pg_boolean_literals(text: str) -> str:
    """Translate true/false outside quoted strings to SQL Server BIT values."""
    parts = re.split(r"('(?:[^']|'')*')", text)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"\btrue\b", "1", parts[i], flags=re.IGNORECASE)
        parts[i] = re.sub(r"\bfalse\b", "0", parts[i], flags=re.IGNORECASE)
    return "".join(parts)


def _select_end(after: str) -> int:
    """Index of the end of the statement/subquery beginning at ``after``.

    The end is the enclosing ``)`` for a scalar subquery, a ``;`` for a
    top-level statement, or the end of the string. Quotes and nested parens
    are respected."""
    depth = 0
    in_str = False
    i = 0
    while i < len(after):
        ch = after[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(after) and after[i + 1] == "'":
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            if depth == 0:
                return i
            depth -= 1
        elif ch == ";" and depth == 0:
            return i
        i += 1
    return len(after)


def _translate_top(text: str) -> str:
    """Rewrite every ``SELECT TOP n`` (leading or inside scalar subqueries)
    into a trailing ``LIMIT n`` at the end of that statement/subquery."""
    out = []
    pos = 0
    for m in re.finditer(r"\bSELECT\s+(?:(DISTINCT)\s+)?TOP\s*(\(\d+\)|\d+)\b", text, re.IGNORECASE):
        out.append(text[pos:m.start()])
        count = m.group(2).strip().strip("()")
        distinct = m.group(1)
        after = text[m.end():]
        end = _select_end(after)
        body = after[:end]
        prefix = "SELECT DISTINCT" if distinct else "SELECT"
        out.append(f"{prefix} {body} LIMIT {count}")
        pos = m.end() + end
    out.append(text[pos:])
    return "".join(out)


def _boolean_column_names(tables: list, source: str) -> set[str]:
    """Lowercased names of columns whose source type is BIT/BOOLEAN."""
    bool_cols: set[str] = set()
    for table in tables or []:
        for col in table.columns:
            base = col.data_type.split("(")[0].strip().upper()
            if base in ("BIT", "BOOLEAN"):
                bool_cols.add(col.name.lower())
    return bool_cols


def _normalize_index_where_columns(where: str, table: Table) -> str:
    """Normalize quoted column references in an index WHERE clause to match
    the actual column names from the table schema.

    MSSQL filter_definition may use different casing than sys.columns,
    causing case mismatches when the expression is quoted for PostgreSQL.
    """
    col_casing: dict[str, str] = {}
    for col in table.columns:
        col_casing[col.name.lower()] = col.name

    def _replace_col(match):
        name = match.group(1)
        canon = col_casing.get(name.lower())
        if canon and canon != name:
            return f'"{canon}"'
        return match.group(0)

    return re.sub(r'"([\w]+)"', _replace_col, where)


def fix_boolean_predicates(expr: str, tables: list, source: str) -> str:
    """Rewrite ``col = 1`` / ``col = (0)`` on BIT/BOOLEAN columns to
    ``col = true`` / ``col = false`` so PostgreSQL doesn't error with
    ``boolean = integer``."""
    bool_cols = _boolean_column_names(tables, source)
    if not bool_cols:
        return expr
    for col in sorted(bool_cols, key=len, reverse=True):
        # parenthesized values, e.g. WHERE ("IsActive"=(1))
        pattern = re.compile(
            rf'("?{re.escape(col)}"?)\s*=\s*\(\s*([01])\s*\)', re.IGNORECASE
        )
        expr = pattern.sub(
            lambda m: f'{m.group(1)} = {"true" if m.group(2) == "1" else "false"}',
            expr,
        )
        # bare values, e.g. WHERE "IsActive" = 1
        pattern = re.compile(
            rf'("?{re.escape(col)}"?)\s*=\s*([01])(?![\d])', re.IGNORECASE
        )
        expr = pattern.sub(
            lambda m: f'{m.group(1)} = {"true" if m.group(2) == "1" else "false"}',
            expr,
        )
    return expr


# ---------------------------------------------------------------------------
# identifier helpers
# ---------------------------------------------------------------------------

def _qident(name: str, target: str) -> str:
    if target == "postgres":
        name = pg_ident(name)
        if "." in name:
            return ".".join(f'"{part}"' for part in name.split("."))
        return f'"{name}"'
    if "." in name:
        return ".".join(f"[{part}]" for part in name.split("."))
    return f"[{name}]"


def _qcols(cols: list[str], target: str) -> str:
    return ", ".join(_qident(c, target) for c in cols)


def _qstr(name: str) -> str:
    """Build a PostgreSQL string literal containing a quoted qualified name,
    e.g. ``'"dbo"."MySeq"'`` for ``setval()``."""
    parts = name.split(".")
    return "'" + ".".join('"' + p.replace('"', '""') + '"' for p in parts) + "'"


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def pg_ident(name: str) -> str:
    """Truncate an identifier to PostgreSQL's 63-byte limit, deduplicating
    via a short hash so distinct long names stay distinct."""
    if len(name.encode("utf-8")) <= _PG_MAX_IDENT:
        return name
    head = name.encode("utf-8")[:_PG_MAX_IDENT - 9].decode("utf-8", "ignore")
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return f"{head}_{digest}"


def _anon_name(table: Table, definition: str) -> str:
    # Fallback name for check constraints whose real name is unavailable:
    # stable per table+definition so several checks on one table never collide
    # and a re-run of the migration doesn't hit a duplicate constraint name.
    base = table.name.replace(".", "_")
    digest = hashlib.md5(definition.encode("utf-8")).hexdigest()[:6]
    return f"{base}_chk_{digest}"


def _qualify_table_refs(text: str, table_names: list[str], target: str) -> str:
    """Qualify bare table references inside view/function/procedure bodies.

    MSSQL bodies reference ``Orders`` and resolve via the default dbo schema;
    PostgreSQL identifiers are case-sensitive so the migrated (quoted) table
    ``"dbo"."Orders"`` would never be found. Every bare reference that matches
    a known table name (case-insensitively, at a word boundary) is rewritten
    to its schema-qualified quoted form. Structural DDL is untouched because
    those identifiers are already quoted.
    """
    for tname in sorted(table_names, key=len, reverse=True):
        if "." in tname:
            # schema.table refs (e.g. `dbo.Orders` from a T-SQL trigger/view
            # body) must be quoted in full, or PostgreSQL lowercases the parts.
            text = re.sub(
                rf"(?<![\w\"\[]){re.escape(tname)}(?![\w.])",
                _qident(tname, target),
                text,
                flags=re.IGNORECASE,
            )
    for tname in sorted(table_names, key=len, reverse=True):
        bare = tname.split(".")[-1]
        qualified = _qident(tname, target)
        text = re.sub(
            rf"(?<![.\w\"\[])\b{re.escape(bare)}\b(?![.\w\"\[])",
            qualified,
            text,
            flags=re.IGNORECASE,
        )
    return text


_COL_MASK = 0  # "\x00n\x00" marker
_IDENT_MASK = 1  # "\x01n\x01" marker
_SQL_KEYWORDS = frozenset(
    "select from where join on as group by order having sum count avg min max top distinct "
    "into insert update delete values and or not null is like between in exists case when then "
    "else end inner outer left right full cross union all set table create alter drop index view "
    "procedure function trigger if while begin declare print raiserror throw waitfor commit rollback "
    "exec execute use default primary foreign references constraint unique check identity asc desc "
    "with collate convert cast return returns returning returning limit offset".split()
)


def _qualify_body_refs(text: str, tables: list, target: str) -> str:
    """Rewrite body expressions so unquoted MSSQL references resolve in PostgreSQL.

    MSSQL bodies are written case-insensitively (``FROM Orders``, ``o.CustomerID``);
    PostgreSQL quoted identifiers are case-sensitive. We (1) qualify bare table
    names to their quoted schema-qualified form and (2) quote column references
    using the original casing captured from the source schema. String literals
    and already-quoted identifiers are masked so they are never rewritten.
    """
    if target != "postgres":
        # PostgreSQL extraction already preserves identifiers with quotes and
        # _translate_expr converts those to brackets. Broadly replacing bare
        # names here corrupts SELECT aliases (Category -> dbo.Category).
        return text

    masked, stash = _mask(text)
    masked = _qualify_table_refs(masked, [t.name for t in tables], target)

    col_casing: dict[str, str] = {}
    for table in tables:
        for col in table.columns:
            col_casing.setdefault(col.name.lower(), col.name)

    # Bare references to a function's own parameters must stay unquoted so
    # PL/pgSQL resolves them as variables rather than ambiguous columns.
    param_names = _signature_param_names(masked)

    def _repl(match) -> str:
        alias, col = match.group(1), match.group(2)
        if col is None:
            alias, col = "", match.group(3)
            if col.lower() in param_names:
                return match.group(0)
        if col.lower() in _SQL_KEYWORDS:
            return match.group(0)
        canon = col_casing.get(col.lower())
        if not canon:
            return match.group(0)
        return f'{alias}."{canon}"' if alias else f'"{canon}"'

    masked = re.sub(r"(\b\w+)\.([A-Za-z_]\w*)|(?<![\w.\"])([A-Za-z_]\w*)", _repl, masked)
    return _unmask(masked, stash)


def _signature_param_names(statement: str) -> set[str]:
    """Extract parameter names from `FUNCTION name(...)` signatures."""
    m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+\w+(?:\.\w+)?\s*\((.*?)\)", statement,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return set()
    names: set[str] = set()
    for part in m.group(1).split(","):
        pm = re.match(r'^\s*(?:(?:OUT|IN)\s+)?"?([\w]+)"?\s+', part)
        if pm:
            names.add(pm.group(1).lower())
    return names


def _mask(text: str) -> tuple[str, list[str]]:
    """Hide string literals and quoted identifiers so regex rewrites skip them."""
    stash: list[str] = []

    def _s1(m):
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    def _s2(m):
        stash.append(m.group(0))
        return f"\x01{len(stash) - 1}\x01"

    text = re.sub(r"'([^']*)'", _s1, text)
    text = re.sub(r'"([^"]*)"', _s2, text)
    # Column/table aliases introduced by `AS` are local to the statement and
    # must never be rewritten as qualified identifiers (they aren't schema
    # objects). Masked before the table-ref rewrite below.
    text = re.sub(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\b", _s2, text, flags=re.IGNORECASE)
    return text, stash


def _unmask(text: str, stash: list[str]) -> str:
    for i, s in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", s).replace(f"\x01{i}\x01", s)
    return text
