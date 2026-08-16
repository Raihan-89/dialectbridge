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
class CheckConstraint:
    name: str
    definition: str


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
    included_columns: list[str] = field(default_factory=list)  # INCLUDE (...)
    is_clustered: bool = False
    index_type: str | None = None         # None | "columnstore" | "spatial" | "xml" | "fulltext"


@dataclass
class PartitionChild:
    """A concrete child partition of a partitioned table."""
    name: str
    bounds: str                           # PG: "FOR VALUES FROM (..) TO (..)" / "FOR VALUES IN (..)"


@dataclass
class Table:
    name: str                             # qualified, e.g. "dbo.Employees"
    columns: list[Column]
    primary_key: Constraint | None = None
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    unique_constraints: list[Constraint] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    check_constraints: list[CheckConstraint] = field(default_factory=list)
    is_partitioned: bool = False
    partition_type: str | None = None     # "range" | "list"
    partition_key: str | None = None
    partition_children: list[PartitionChild] = field(default_factory=list)
    is_temporal: bool = False
    temporal_history_table: str | None = None
    is_graph: bool = False
    graph_kind: str | None = None         # "node" | "edge"


@dataclass
class View:
    name: str
    definition: str
    is_materialized: bool = False
    indexes: list[Index] = field(default_factory=list)


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
    table: str | None
    timing: str                           # BEFORE | AFTER | INSTEAD OF
    events: list[str]                     # DML: INSERT/UPDATE/DELETE; DDL: tags like CREATE_TABLE
    definition: str
    is_ddl: bool = False
    ddl_scope: str = "object"             # "object" | "database" | "server"


@dataclass
class Sequence:
    name: str
    start_value: int
    increment: int = 1
    owned_by: str | None = None           # "table.column" this sequence feeds
    current_value: int | None = None      # last value; None if never used
    data_type: str | None = None          # native type (MSSQL AS type; PG data_type)
    is_cycling: bool = False


@dataclass
class UserType:
    """User-defined type: alias type, domain, enum, composite, table type or CLR type."""
    name: str
    kind: str                             # "domain" | "alias" | "enum" | "composite" | "table_type" | "clr"
    base_type: str | None = None
    default: str | None = None
    nullable: bool = True
    constraints: list[str] = field(default_factory=list)   # CHECK definitions
    values: list[str] = field(default_factory=list)        # enum labels
    columns: list[Column] = field(default_factory=list)    # table/composite attributes


@dataclass
class Synonym:
    name: str
    target_object: str
    target_kind: str                      # "table" | "view" | "procedure" | "function" | ...


@dataclass
class Principal:
    """A database-level user or role (security principal)."""
    name: str
    kind: str                             # "user" | "role"
    is_system: bool = False
    member_of: list[str] = field(default_factory=list)


@dataclass
class Permission:
    principal: str
    securable: str                        # "table" | "view" | "procedure" | "function" | "schema" | "database"
    object_name: str
    action: str                           # SELECT | INSERT | UPDATE | DELETE | EXECUTE | ...
    grant_type: str = "GRANT"             # GRANT | DENY | REVOKE
    with_grant: bool = False


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
    types: list[UserType] = field(default_factory=list)
    synonyms: list[Synonym] = field(default_factory=list)
    users: list[Principal] = field(default_factory=list)
    roles: list[Principal] = field(default_factory=list)
    permissions: list[Permission] = field(default_factory=list)
    collation: str | None = None
    warnings: list[str] = field(default_factory=list)

    def table_by_name(self, name: str) -> Table | None:
        for table in self.tables:
            if table.name.lower() == name.lower():
                return table
        return None

    def type_by_name(self, name: str) -> UserType | None:
        """Find a user-defined type by qualified name or bare (last) segment."""
        lowered = name.lower()
        for ut in self.types:
            if ut.name.lower() == lowered:
                return ut
            if ut.name.rsplit(".", 1)[-1].lower() == lowered:
                return ut
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
            "views": [_view_to_dict(v) for v in self.views],
            "functions": [_routine_to_dict(r) for r in self.functions],
            "procedures": [_routine_to_dict(r) for r in self.procedures],
            "triggers": [_trigger_to_dict(t) for t in self.triggers],
            "sequences": [_seq_to_dict(s) for s in self.sequences],
            "types": [_type_to_dict(t) for t in self.types],
            "synonyms": [{"name": s.name, "target_object": s.target_object, "target_kind": s.target_kind} for s in self.synonyms],
            "users": [_principal_to_dict(p) for p in self.users],
            "roles": [_principal_to_dict(p) for p in self.roles],
            "permissions": [_permission_to_dict(p) for p in self.permissions],
            "collation": self.collation,
            "warnings": self.warnings,
        }


def _view_to_dict(v: View) -> dict:
    return {
        "name": v.name,
        "definition": v.definition,
        "is_materialized": v.is_materialized,
        "indexes": [_index_to_dict(i) for i in v.indexes],
    }


def _index_to_dict(i: Index) -> dict:
    return {
        "name": i.name,
        "columns": i.columns,
        "unique": i.unique,
        "where": i.where,
        "included_columns": i.included_columns,
        "is_clustered": i.is_clustered,
        "index_type": i.index_type,
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
        "indexes": [_index_to_dict(i) for i in t.indexes],
        "check_constraints": [
            {"name": c.name, "definition": c.definition} for c in t.check_constraints
        ],
        "is_partitioned": t.is_partitioned,
        "partition_type": t.partition_type,
        "partition_key": t.partition_key,
        "partition_children": [
            {"name": c.name, "bounds": c.bounds} for c in t.partition_children
        ],
        "is_temporal": t.is_temporal,
        "temporal_history_table": t.temporal_history_table,
        "is_graph": t.is_graph,
        "graph_kind": t.graph_kind,
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
        "is_ddl": t.is_ddl,
        "ddl_scope": t.ddl_scope,
    }


def _seq_to_dict(s: Sequence) -> dict:
    return {
        "name": s.name,
        "start_value": s.start_value,
        "increment": s.increment,
        "owned_by": s.owned_by,
        "current_value": s.current_value,
        "data_type": s.data_type,
        "is_cycling": s.is_cycling,
    }


def _type_to_dict(t: UserType) -> dict:
    return {
        "name": t.name,
        "kind": t.kind,
        "base_type": t.base_type,
        "default": t.default,
        "nullable": t.nullable,
        "constraints": t.constraints,
        "values": t.values,
        "columns": [
            {"name": c.name, "data_type": c.data_type, "nullable": c.nullable,
             "default": c.default}
            for c in t.columns
        ],
    }


def _principal_to_dict(p: Principal) -> dict:
    return {
        "name": p.name,
        "kind": p.kind,
        "is_system": p.is_system,
        "member_of": p.member_of,
    }


def _permission_to_dict(p: Permission) -> dict:
    return {
        "principal": p.principal,
        "securable": p.securable,
        "object_name": p.object_name,
        "action": p.action,
        "grant_type": p.grant_type,
        "with_grant": p.with_grant,
    }
