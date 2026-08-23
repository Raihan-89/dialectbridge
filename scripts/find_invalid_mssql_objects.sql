/*
  Which SQL Server views/routines reference an object that no longer exists.

  DialectBridge cannot recreate these as real objects on PostgreSQL: the
  reference is already unresolvable in SQL Server itself (SELECT * from such a
  view fails there with "Invalid object name"). Views in this list are migrated
  as dependency-free compatibility views that keep the exact column contract.

  Run this against the source database before a migration to see the full list.
*/
SELECT
    o.type_desc                                   AS referencing_kind,
    OBJECT_SCHEMA_NAME(d.referencing_id) + '.'
        + OBJECT_NAME(d.referencing_id)           AS referencing_object,
    ISNULL(d.referenced_schema_name, 'dbo') + '.'
        + d.referenced_entity_name                AS missing_object
FROM sys.sql_expression_dependencies d
JOIN sys.objects o
    ON o.object_id = d.referencing_id
WHERE d.referenced_id IS NULL
  AND d.referenced_server_name IS NULL
  AND d.referenced_database_name IS NULL
  AND d.is_ambiguous = 0
  AND OBJECT_ID(ISNULL(d.referenced_schema_name, 'dbo') + '.' + d.referenced_entity_name) IS NULL
  AND o.is_ms_shipped = 0
  -- 'inserted'/'deleted' are trigger pseudo-tables, not real objects
  AND d.referenced_entity_name NOT IN ('inserted', 'deleted')
ORDER BY o.type_desc, referencing_object, missing_object;
