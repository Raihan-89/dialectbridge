"""
Custom data type mapping rules for SQL Server <-> PostgreSQL conversion.
Covers the FULL SQL Server data type inventory (~27 types).
sqlglot handles many of these correctly already (not listed here to avoid
double-processing) — this file only contains types sqlglot gets wrong,
leaves unchanged, or has no concept of at all.
"""

# T-SQL -> PostgreSQL type overrides
# (only types sqlglot does NOT already convert correctly)
MSSQL_TO_POSTGRES_TYPE_OVERRIDES = {
    "TINYINT": "SMALLINT",
    "BIT": "BOOLEAN",
    "MONEY": "NUMERIC(19,4)",
    "SMALLMONEY": "NUMERIC(10,4)",
    "NTEXT": "TEXT",
    "DATETIME2": "TIMESTAMP",
    "SMALLDATETIME": "TIMESTAMP",
    "DATETIMEOFFSET": "TIMESTAMPTZ",
    "BINARY": "BYTEA",
    "VARBINARY": "BYTEA",
    "IMAGE": "BYTEA",
    "UNIQUEIDENTIFIER": "UUID",
}

# PostgreSQL -> T-SQL type overrides (for reverse conversion)
POSTGRES_TO_MSSQL_TYPE_OVERRIDES = {
    "BOOLEAN": "BIT",
    "UUID": "UNIQUEIDENTIFIER",
    "BYTEA": "VARBINARY(MAX)",
    "SERIAL": "INT IDENTITY(1,1)",
    "BIGSERIAL": "BIGINT IDENTITY(1,1)",
    "TEXT": "NVARCHAR(MAX)",
    "JSON": "NVARCHAR(MAX)",
    "JSONB": "NVARCHAR(MAX)",
    "TIMESTAMPTZ": "DATETIMEOFFSET",
    "DOUBLE PRECISION": "FLOAT",
}

# Types with NO clean equivalent — must be flagged for manual review,
# never silently converted (silent wrong conversion is worse than no conversion)
MANUAL_REVIEW_REQUIRED = {
    "mssql_to_postgres": [
        "HIERARCHYID",     # no PG equivalent - typically flattened to TEXT/ints manually
        "SQL_VARIANT",     # no PG equivalent - dynamic typing not supported
        "GEOGRAPHY",       # requires PostGIS extension, not a drop-in type swap
        "GEOMETRY",        # requires PostGIS extension, not a drop-in type swap
        "ROWVERSION",      # PG has no auto-updating binary version column - needs trigger
        "CURSOR",          # variable type, not a column type - needs procedural rewrite
    ],
    "postgres_to_mssql": [
        "ARRAY",
        "HSTORE",
        "INT4RANGE", "INT8RANGE", "NUMRANGE", "TSRANGE", "TSTZRANGE", "DATERANGE",
        "INET",
        "CIDR",
        "MACADDR",
    ],
}

# Boolean literal conversion: SQL Server uses 1/0, PostgreSQL uses true/false.
# This is a VALUE-level fix, not a type-level one — needed because a plain
# type-name regex swap (BIT -> BOOLEAN) leaves "DEFAULT 1" behind, which is
# invalid syntax once the column is BOOLEAN.
BIT_DEFAULT_VALUE_MAP = {
    "1": "true",
    "0": "false",
}