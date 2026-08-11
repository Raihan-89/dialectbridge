"""
Conversion service facade: the single entry point the API and web UI use.

Routes a conversion request to the right translator based on the statement
type. Currently DDL and DML both flow through the sqlglot-based translator
in `engine.translators.ddl_translator` (sqlglot handles DML — SELECT/INSERT/
UPDATE/DELETE — well; the DDL module's type/syntax fixes are harmless when
applied to DML output). Procedural statements (stored procedures, functions,
triggers) are not translatable yet and raise a clear error instead of
silently producing broken SQL.
"""
from engine.translators.ddl_translator import convert_ddl

# Maps the model's Direction choice to the (read, write) dialect args
# convert_ddl() expects.
DIRECTION_TO_DIALECTS = {
    "mssql_to_postgres": ("tsql", "postgres"),
    "postgres_to_mssql": ("postgres", "tsql"),
}

# Statement types that can flow through the current sqlglot-based pipeline.
_SUPPORTED_STATEMENT_TYPES = {"ddl", "dml"}


class UnsupportedStatementTypeError(Exception):
    """Raised when a statement type has no translator wired up yet."""


def convert_sql(source_sql: str, direction: str, statement_type: str):
    """
    Convert `source_sql` from one dialect to the other.

    Returns a ConversionResult(sql, warnings). Raises
    UnsupportedStatementTypeError for statement types without a translator.
    """
    try:
        read_dialect, write_dialect = DIRECTION_TO_DIALECTS[direction]
    except KeyError:
        raise ValueError(f"Unknown conversion direction: {direction!r}")

    if statement_type not in _SUPPORTED_STATEMENT_TYPES:
        raise UnsupportedStatementTypeError(
            f"Conversion for statement_type='{statement_type}' isn't implemented yet. "
            f"Supported types: {sorted(_SUPPORTED_STATEMENT_TYPES)}."
        )

    return convert_ddl(
        source_sql, source_dialect=read_dialect, target_dialect=write_dialect
    )
