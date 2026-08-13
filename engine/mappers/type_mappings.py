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

# Best-effort collation mapping. PostgreSQL collations are locale-based while
# SQL Server uses its own Windows catalog names, so there is no exact 1:1 map;
# these are the common pairings. Anything unknown is surfaced as a warning and
# the column is created without COLLATE (target database collation applies).
# Lowercased source name -> target collation name (always lowercase compare).
MSSQL_TO_PG_COLLATIONS = {
    "sql_latin1_general_cp1_ci_as": "en_US",
    "sql_latin1_general_cp1_cs_as": "C",
    "latin1_general_ci_as": "en_US",
    "latin1_general_cs_as": "C",
    "sql_latin1_general_cp1250_ci_as": "en_US",
    "sql_latin1_general_cp1250_cs_as": "C",
    "czech_ci_as": "cs_CZ.UTF-8",
    "czech_cs_as": "cs_CZ.UTF-8",
    "finnish_swedish_ci_as": "fi_FI.UTF-8",
    "finnish_swedish_cs_as": "fi_FI.UTF-8",
    "french_ci_as": "fr_FR.UTF-8",
    "french_cs_as": "fr_FR.UTF-8",
    "german_phonebook_ci_as": "de_DE.UTF-8",
    "german_ci_as": "de_DE.UTF-8",
    "german_cs_as": "de_DE.UTF-8",
    "japanese_ci_as": "ja_JP.UTF-8",
    "japanese_cs_as": "ja_JP.UTF-8",
    "polish_ci_as": "pl_PL.UTF-8",
    "polish_cs_as": "pl_PL.UTF-8",
    "portuguese_ci_as": "pt_PT.UTF-8",
    "portuguese_cs_as": "pt_PT.UTF-8",
    "spanish_ci_as": "es_ES.UTF-8",
    "spanish_cs_as": "es_ES.UTF-8",
    "swedish_fi_ci_as": "sv_FI.UTF-8",
    "turkish_ci_as": "tr_TR.UTF-8",
    "turkish_cs_as": "tr_TR.UTF-8",
    "ukrainian_ci_as": "uk_UA.UTF-8",
    "ukrainian_cs_as": "uk_UA.UTF-8",
}

# PostgreSQL collation -> T-SQL (reverse direction). Lowercased key.
PG_TO_MSSQL_COLLATIONS = {
    "c": "Latin1_General_CS_AS",
    "c.utf-8": "Latin1_General_CS_AS",
    "en_us": "Latin1_General_CI_AS",
    "en_us.utf-8": "Latin1_General_CI_AS",
    "en_gb": "Latin1_General_CI_AS",
    "en_gb.utf-8": "Latin1_General_CI_AS",
    "cs_cz": "Czech_CI_AS",
    "cs_cz.utf-8": "Czech_CI_AS",
    "de_de": "German_CI_AS",
    "de_de.utf-8": "German_CI_AS",
    "fr_fr": "French_CI_AS",
    "fr_fr.utf-8": "French_CI_AS",
    "ja_jp": "Japanese_CI_AS",
    "ja_jp.utf-8": "Japanese_CI_AS",
    "pl_pl": "Polish_CI_AS",
    "pl_pl.utf-8": "Polish_CI_AS",
    "pt_pt": "Portuguese_CI_AS",
    "pt_pt.utf-8": "Portuguese_CI_AS",
    "es_es": "Modern_Spanish_CI_AS",
    "es_es.utf-8": "Modern_Spanish_CI_AS",
    "tr_tr": "Turkish_CI_AS",
    "tr_tr.utf-8": "Turkish_CI_AS",
}

# MSSQL DDL trigger events -> PostgreSQL event-trigger tags (space form).
# Used to build the WHEN TAG IN (...) clause. Unmapped events are warned.
MSSQL_DDL_EVENT_TO_PG_TAG = {
    "CREATE_TABLE": "CREATE TABLE",
    "ALTER_TABLE": "ALTER TABLE",
    "DROP_TABLE": "DROP TABLE",
    "CREATE_VIEW": "CREATE VIEW",
    "ALTER_VIEW": "ALTER VIEW",
    "DROP_VIEW": "DROP VIEW",
    "CREATE_FUNCTION": "CREATE FUNCTION",
    "ALTER_FUNCTION": "ALTER FUNCTION",
    "DROP_FUNCTION": "DROP FUNCTION",
    "CREATE_PROCEDURE": "CREATE FUNCTION",
    "ALTER_PROCEDURE": "ALTER FUNCTION",
    "DROP_PROCEDURE": "DROP FUNCTION",
    "CREATE_TRIGGER": "CREATE TRIGGER",
    "ALTER_TRIGGER": "ALTER TRIGGER",
    "DROP_TRIGGER": "DROP TRIGGER",
    "CREATE_INDEX": "CREATE INDEX",
    "ALTER_INDEX": "ALTER INDEX",
    "DROP_INDEX": "DROP INDEX",
    "CREATE_TYPE": "CREATE TYPE",
    "CREATE_SCHEMA": "CREATE SCHEMA",
    "ALTER_SCHEMA": "ALTER SCHEMA",
    "DROP_SCHEMA": "DROP SCHEMA",
    "CREATE_SEQUENCE": "CREATE SEQUENCE",
    "ALTER_SEQUENCE": "ALTER SEQUENCE",
    "DROP_SEQUENCE": "DROP SEQUENCE",
}

# PostgreSQL event-trigger tags -> MSSQL DDL trigger events (reverse).
PG_TAG_TO_MSSQL_DDL_EVENT = {v: k for k, v in MSSQL_DDL_EVENT_TO_PG_TAG.items()}