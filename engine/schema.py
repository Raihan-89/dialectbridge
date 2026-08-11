"""
Normalized, dialect-independent schema representation.

Both extractors (MSSQL, PostgreSQL) produce this same structure, and the
conversion layer turns it into target-dialect DDL. Keeping one intermediate
format means the convert/apply/migrate pipeline never has to know which
database a table came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Column:
    name: str
    data_type: str                       # source-native, e.g. "NVARCHAR(50)", "DECIMAL(19,4)"
    nullable: bool = True
    default: str | None = None           # raw default expression, e.g. "NEWID()", "0"
    is_identity: bool = False
    identity_seed: int | None = None
    identity_increment: int | None = None
    is_computed: bool = False
    computed_definition: str | None = None
    collation: str | None = None
    comment: str | None = None


@dataclass
class Constraint:
    name: str
    columns: list[str]


@dataclass
class ForeignKey:
    name: str
    columns: list[str]
    ref_table: str                        # qualified name of referenced table
    ref_columns: list[str]
    on_update: str = "NO ACTION"
    on_delete: str = "NO ACTION"


@dataclass
class Index:
    name: str
    columns: list[str]
    unique: bool = False
    where: str | None = None              # filtered index (SQL Server)


@dataclass
class Table:
    name: str                             # qualified, e.g. "dbo.Employees"
    columns: list[Column]
    primary_key: Constraint | None = None
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    unique_constraints: list[Constraint] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    check_constraints: list[str] = field(default_factory=list)


@dataclass
class View:
    name: str
    definition: str


@dataclass
class Routine:
    """Stored procedure or user-defined function (scalar / table-valued)."""
    name: str
    kind: str                             # "procedure" | "function"
    definition: str                       # full CREATE source
    parameters: str | None = None         # raw parameter list text
    returns: str | None = None            # functions only


@dataclass
class Trigger:
    name: str
    table: str
    timing: str                           # BEFORE | AFTER | INSTEAD OF
    events: list[str]                     # subset of INSERT/UPDATE/DELETE
    definition: str


@dataclass
class Sequence:
    name: str
    start_value: int
    increment: int = 1
    owned_by: str | None = None           # "table.column" this sequence feeds


@dataclass
class Database:
    name: str
    dialect: str                          # "tsql" | "postgres"
    tables: list[Table] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    functions: list[Routine] = field(default_factory=list)
    procedures: list[Routine] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)
    sequences: list[Sequence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def table_by_name(self, name: str) -> Table | None:
        for table in self.tables:
            if table.name.lower() == name.lower():
                return table
        return None

    def all_tables_in_dependency_order(self) -> list[Table]:
        """Topological sort: referenced tables come before referencing ones."""
        # Build qualified-name -> Table map (case-insensitive).
        by_name = {t.name.lower(): t for t in self.tables}
        # Edges: referencing table depends on referenced table.
        order: list[Table] = []
        state: dict[str, int] = {}   # 0=unvisited 1=visiting 2=done

        def visit(table: Table):
            key = table.name.lower()
            if state.get(key) == 2:
                return
            if state.get(key) == 1:
                return  # cycle (self-ref FK) — keep original position
            state[key] = 1
            for fk in table.foreign_keys:
                dep = by_name.get(fk.ref_table.lower())
                if dep is not None and dep is not table:
                    visit(dep)
            state[key] = 2
            order.append(table)

        for table in self.tables:
            visit(table)
        return order

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "dialect": self.dialect,
            "tables": [_table_to_dict(t) for t in self.tables],
            "views": [{"name": v.name, "definition": v.definition} for v in self.views],
            "functions": [_routine_to_dict(r) for r in self.functions],
            "procedures": [_routine_to_dict(r) for r in self.procedures],
            "triggers": [_trigger_to_dict(t) for t in self.triggers],
            "sequences": [_seq_to_dict(s) for s in self.sequences],
            "warnings": self.warnings,
        }


def _table_to_dict(t: Table) -> dict:
    return {
        "name": t.name,
        "columns": [
            {
                "name": c.name,
                "data_type": c.data_type,
                "nullable": c.nullable,
                "default": c.default,
                "is_identity": c.is_identity,
                "identity_seed": c.identity_seed,
                "identity_increment": c.identity_increment,
                "is_computed": c.is_computed,
                "computed_definition": c.computed_definition,
                "collation": c.collation,
                "comment": c.comment,
            }
            for c in t.columns
        ],
        "primary_key": _constraint_to_dict(t.primary_key),
        "foreign_keys": [
            {
                "name": fk.name,
                "columns": fk.columns,
                "ref_table": fk.ref_table,
                "ref_columns": fk.ref_columns,
                "on_update": fk.on_update,
                "on_delete": fk.on_delete,
            }
            for fk in t.foreign_keys
        ],
        "unique_constraints": [_constraint_to_dict(c) for c in t.unique_constraints],
        "indexes": [
            {"name": i.name, "columns": i.columns, "unique": i.unique, "where": i.where}
            for i in t.indexes
        ],
        "check_constraints": t.check_constraints,
    }


def _constraint_to_dict(c: Constraint | None) -> dict | None:
    if c is None:
        return None
    return {"name": c.name, "columns": c.columns}


def _routine_to_dict(r: Routine) -> dict:
    return {
        "name": r.name,
        "kind": r.kind,
        "definition": r.definition,
        "parameters": r.parameters,
        "returns": r.returns,
    }


def _trigger_to_dict(t: Trigger) -> dict:
    return {
        "name": t.name,
        "table": t.table,
        "timing": t.timing,
        "events": t.events,
        "definition": t.definition,
    }


def _seq_to_dict(s: Sequence) -> dict:
    return {
        "name": s.name,
        "start_value": s.start_value,
        "increment": s.increment,
        "owned_by": s.owned_by,
    }
