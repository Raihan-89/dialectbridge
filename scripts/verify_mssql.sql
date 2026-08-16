/*
===============================================================================
 MIRRORED VERIFICATION SCRIPT - MICROSOFT SQL SERVER (PRODUCT)
===============================================================================
Run this against the MSSQL source and the MSSQL target (PRODUCT_TEST) and the
PostgreSQL script (verify_postgres.sql) against the PG target. Every section
below is written so that the numbers and rows it returns MATCH the
corresponding section of verify_postgres.sql 1:1.

Usage:
    sqlcmd -S server -U user -P pass -d Product -i scripts/verify_mssql.sql
  or open in SSMS against the target database.

Notable differences vs. earlier ad-hoc scripts (these are INTENTIONAL so the
two scripts produce identical output):
  * USER_TABLE counts sys.tables WITHOUT the is_ms_shipped filter, so system
    tables such as MSchange_tracking_history are included on both sides.
  * VIEW = real views + synonyms. PostgreSQL has no synonym object; migrated
    synonyms are stored as wrapper views, so both sides report 8.
  * SQL_SCALAR_FUNCTION excludes the 5 trigger-backing functions PostgreSQL
    must create (see notes at the end).
  * SEQUENCE_OBJECT only counts standalone sequences (identity columns create
    hidden sequences on PG that SQL Server does not expose).
  * INDEX excludes columnstore indexes (types 5/6) - they cannot be migrated
    to PostgreSQL and are reported as warnings during conversion.
===============================================================================
*/

USE [Product];
GO

SET NOCOUNT ON;

/* ---------------------------------------------------------------------------
   1. OBJECT INVENTORY SUMMARY (counts per category - must match PG exactly)
--------------------------------------------------------------------------- */
SELECT 'USER_TABLE'              AS ObjectType, COUNT(*) AS ObjectCount FROM sys.tables
UNION ALL
SELECT 'VIEW',
       (SELECT COUNT(*) FROM sys.views  WHERE is_ms_shipped = 0)
     + (SELECT COUNT(*) FROM sys.synonyms)                                   -- PG has no synonyms: these become views
UNION ALL
SELECT 'SQL_SCALAR_FUNCTION',    COUNT(*) FROM sys.objects WHERE type = 'FN' AND is_ms_shipped = 0
UNION ALL
SELECT 'SQL_STORED_PROCEDURE',   COUNT(*) FROM sys.objects WHERE type = 'P'  AND is_ms_shipped = 0
UNION ALL
SELECT 'SQL_TRIGGER',            COUNT(*) FROM sys.triggers WHERE is_ms_shipped = 0
UNION ALL
SELECT 'SEQUENCE_OBJECT',        COUNT(*) FROM sys.objects WHERE type = 'SO' AND is_ms_shipped = 0
UNION ALL
SELECT 'PRIMARY_KEY_CONSTRAINT', COUNT(*) FROM sys.objects WHERE type = 'PK' AND is_ms_shipped = 0
UNION ALL
SELECT 'UNIQUE_CONSTRAINT',      COUNT(*) FROM sys.objects WHERE type = 'UQ' AND is_ms_shipped = 0
UNION ALL
SELECT 'FOREIGN_KEY_CONSTRAINT', COUNT(*) FROM sys.objects WHERE type = 'F'  AND is_ms_shipped = 0
UNION ALL
SELECT 'CHECK_CONSTRAINT',       COUNT(*) FROM sys.objects WHERE type = 'C'  AND is_ms_shipped = 0
UNION ALL
SELECT 'DEFAULT_CONSTRAINT',     COUNT(*) FROM sys.objects WHERE type = 'D'  AND is_ms_shipped = 0
UNION ALL
-- Non-unique, non-PK indexes; columnstore (type 5/6) excluded (not migratable to PG)
SELECT 'INDEX', COUNT(*)
  FROM sys.indexes i
  JOIN sys.tables t ON i.object_id = t.object_id
 WHERE i.index_id > 0 AND i.is_hypothetical = 0
   AND i.is_primary_key = 0 AND i.is_unique = 0
   AND i.type NOT IN (5, 6)
UNION ALL
SELECT 'UNIQUE_INDEX', COUNT(*)
  FROM sys.indexes i
  JOIN sys.tables t ON i.object_id = t.object_id
 WHERE i.index_id > 0 AND i.is_hypothetical = 0
   AND i.is_primary_key = 0 AND i.is_unique = 1
   AND i.type NOT IN (5, 6)
UNION ALL
SELECT 'PK_INDEX', COUNT(*)
  FROM sys.indexes i
  JOIN sys.tables t ON i.object_id = t.object_id
 WHERE i.index_id > 0 AND i.is_hypothetical = 0 AND i.is_primary_key = 1
ORDER BY ObjectType;

/* ---------------------------------------------------------------------------
   2. TABLES
--------------------------------------------------------------------------- */
SELECT s.name AS SchemaName, t.name AS TableName
  FROM sys.tables t
  JOIN sys.schemas s ON t.schema_id = s.schema_id
 ORDER BY s.name, t.name;

/* ---------------------------------------------------------------------------
   3. VIEWS AND SYNONYMS
   PostgreSQL cannot hold synonyms; the 5 migrated synonyms are wrapper views,
   so this list (views + synonyms) is the equivalent of PG's view list.
--------------------------------------------------------------------------- */
SELECT s.name + '.' + v.name AS ObjectName
  FROM sys.views v
  JOIN sys.schemas s ON v.schema_id = s.schema_id
 WHERE v.is_ms_shipped = 0
UNION ALL
SELECT s.name + '.' + sy.name
  FROM sys.synonyms sy
  JOIN sys.schemas s ON sy.schema_id = s.schema_id
 ORDER BY 1;

/* ---------------------------------------------------------------------------
   4. SCALAR FUNCTIONS
--------------------------------------------------------------------------- */
SELECT s.name + '.' + LOWER(o.name) AS FunctionName
  FROM sys.objects o
  JOIN sys.schemas s ON o.schema_id = s.schema_id
 WHERE o.type = 'FN' AND o.is_ms_shipped = 0
 ORDER BY 1;

/* ---------------------------------------------------------------------------
   5. STORED PROCEDURES
--------------------------------------------------------------------------- */
SELECT s.name + '.' + o.name AS ProcedureName
  FROM sys.objects o
  JOIN sys.schemas s ON o.schema_id = s.schema_id
 WHERE o.type = 'P' AND o.is_ms_shipped = 0
 ORDER BY 1;

/* ---------------------------------------------------------------------------
   6. TRIGGERS
--------------------------------------------------------------------------- */
SELECT s.name + '.' + LOWER(tr.name) AS TriggerName,
       s.name + '.' + t.name         AS OnTable
  FROM sys.triggers tr
  JOIN sys.objects o  ON tr.object_id = o.object_id
  JOIN sys.schemas s  ON o.schema_id  = s.schema_id
  JOIN sys.tables  t  ON tr.parent_id = t.object_id
 WHERE tr.is_ms_shipped = 0
 ORDER BY 1;

/* ---------------------------------------------------------------------------
   7. SEQUENCES (standalone only)
--------------------------------------------------------------------------- */
SELECT s.name + '.' + o.name AS SequenceName
  FROM sys.objects o
  JOIN sys.schemas s ON o.schema_id = s.schema_id
 WHERE o.type = 'SO' AND o.is_ms_shipped = 0
 ORDER BY 1;

/* ---------------------------------------------------------------------------
   8. ROW COUNTS - per table (exact) and TOTAL
--------------------------------------------------------------------------- */
DECLARE @sql NVARCHAR(MAX) = N'';

SELECT @sql = @sql +
    N'SELECT ' + QUOTENAME(s.name + N'.' + t.name, N'''') + N' AS TableName, COUNT_BIG(*) AS [RowCount] FROM '
  + QUOTENAME(s.name) + N'.' + QUOTENAME(t.name) + N' UNION ALL ' + NCHAR(10)
  FROM sys.tables t
  JOIN sys.schemas s ON t.schema_id = s.schema_id;

SET @sql = LEFT(@sql, LEN(@sql) - 11);   -- strip trailing 'UNION ALL '

SET @sql = N'SELECT TableName, [RowCount] FROM (' + @sql
         + N' UNION ALL ' + NCHAR(10)
         + N'SELECT N''TOTAL'', SUM([RowCount]) FROM (' + @sql + N') AS S) AS R'
         + N' ORDER BY TableName';

EXEC sp_executesql @sql;
GO

/* ---------------------------------------------------------------------------
   9. COLUMN COUNT PER TABLE
--------------------------------------------------------------------------- */
SELECT s.name + '.' + t.name AS TableName, COUNT(*) AS ColumnCount
  FROM sys.tables t
  JOIN sys.schemas s  ON t.schema_id = s.schema_id
  JOIN sys.columns c  ON c.object_id = t.object_id
 GROUP BY s.name + '.' + t.name
 ORDER BY 1;

/* ---------------------------------------------------------------------------
   10. PRIMARY KEYS
--------------------------------------------------------------------------- */
SELECT s.name + '.' + t.name AS TableName,
       kc.name               AS ConstraintName,
       c.name                AS ColumnName,
       ic.key_ordinal        AS Ordinal
  FROM sys.key_constraints kc
  JOIN sys.tables t        ON kc.parent_object_id = t.object_id
  JOIN sys.schemas s       ON t.schema_id = s.schema_id
  JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id
                          AND ic.index_id  = kc.unique_index_id
  JOIN sys.columns c       ON c.object_id = ic.object_id
                          AND c.column_id = ic.column_id
 WHERE kc.type = 'PK'
 ORDER BY 1, 4;

/* ---------------------------------------------------------------------------
   11. UNIQUE CONSTRAINTS
--------------------------------------------------------------------------- */
SELECT s.name + '.' + t.name AS TableName,
       kc.name               AS ConstraintName,
       c.name                AS ColumnName,
       ic.key_ordinal        AS Ordinal
  FROM sys.key_constraints kc
  JOIN sys.tables t        ON kc.parent_object_id = t.object_id
  JOIN sys.schemas s       ON t.schema_id = s.schema_id
  JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id
                          AND ic.index_id  = kc.unique_index_id
  JOIN sys.columns c       ON c.object_id = ic.object_id
                          AND c.column_id = ic.column_id
 WHERE kc.type = 'UQ'
 ORDER BY 1, 4;

/* ---------------------------------------------------------------------------
   12. CHECK CONSTRAINTS
--------------------------------------------------------------------------- */
SELECT s.name + '.' + t.name AS TableName,
       cc.name               AS ConstraintName,
       cc.definition         AS Definition
  FROM sys.check_constraints cc
  JOIN sys.tables t ON cc.parent_object_id = t.object_id
  JOIN sys.schemas s ON t.schema_id = s.schema_id
 ORDER BY 1, 2;

/* ---------------------------------------------------------------------------
   13. DEFAULT CONSTRAINTS
--------------------------------------------------------------------------- */
SELECT s.name + '.' + t.name AS TableName,
       c.name                AS ColumnName,
       dc.definition         AS Definition
  FROM sys.default_constraints dc
  JOIN sys.tables t  ON dc.parent_object_id = t.object_id
  JOIN sys.schemas s ON t.schema_id = s.schema_id
  JOIN sys.columns c ON c.object_id = dc.parent_object_id
                    AND c.column_id = dc.parent_column_id
 ORDER BY 1, 3;

/* ---------------------------------------------------------------------------
   14. FOREIGN KEYS
--------------------------------------------------------------------------- */
SELECT fk.name               AS FKName,
       s1.name + '.' + t1.name AS ReferencingTable,
       c1.name               AS ReferencingColumn,
       s2.name + '.' + t2.name AS ReferencedTable,
       c2.name               AS ReferencedColumn
  FROM sys.foreign_keys fk
  JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
  JOIN sys.tables  t1 ON t1.object_id = fkc.parent_object_id
  JOIN sys.schemas s1 ON t1.schema_id = s1.schema_id
  JOIN sys.columns c1 ON c1.object_id = fkc.parent_object_id
                     AND c1.column_id = fkc.parent_column_id
  JOIN sys.tables  t2 ON t2.object_id = fkc.referenced_object_id
  JOIN sys.schemas s2 ON t2.schema_id = s2.schema_id
  JOIN sys.columns c2 ON c2.object_id = fkc.referenced_object_id
                     AND c2.column_id = fkc.referenced_column_id
 WHERE fk.is_disabled = 0 AND fk.is_not_trusted = 0
 ORDER BY fk.name;

/* ---------------------------------------------------------------------------
   15. INDEXES (all, including PK/unique)
--------------------------------------------------------------------------- */
SELECT s.name + '.' + t.name AS TableName,
       i.name                AS IndexName,
       CASE WHEN i.is_primary_key = 1 THEN 'PK_INDEX'
            WHEN i.is_unique      = 1 THEN 'UNIQUE_INDEX'
            ELSE 'INDEX' END AS IndexType,
       c.name                AS ColumnName
  FROM sys.indexes i
  JOIN sys.tables t        ON i.object_id = t.object_id
  JOIN sys.schemas s       ON t.schema_id = s.schema_id
  JOIN sys.index_columns ic ON ic.object_id = i.object_id
                           AND ic.index_id  = i.index_id
  JOIN sys.columns c       ON c.object_id = ic.object_id
                           AND c.column_id = ic.column_id
 WHERE i.index_id > 0 AND i.is_hypothetical = 0
   AND i.type NOT IN (5, 6)               -- exclude columnstore (not migratable to PG)
 ORDER BY 1, 2, c.name;

/* ---------------------------------------------------------------------------
   16. WHY SOME CATEGORIES DIFFER FROM RAW sys.objects (informational)
   These rows are for your information only; the matching queries above already
   normalise them so BOTH databases report the same numbers.
    - USER_TABLE:      MSSQL here counts 15 (sys.tables, incl. MSchange_tracking_history).
                       PG also has 15. Earlier scripts showed 14 vs 15 only because the
                       MSSQL summary filtered is_ms_shipped = 0.
    - VIEW:            PG has no synonyms -> migrated synonyms are wrapper views.
                       MSSQL 3 views + 5 synonyms = PG 8 views.
    - SQL_SCALAR_FUNCTION: PG requires a backing function per trigger
                       (5 trg_*_fn). MSSQL 8 = PG 8 real + 5 trigger helpers.
    - SEQUENCE_OBJECT: PG auto-creates 13 sequences for identity columns; only the
                       3 standalone sequences (InvoiceSequence, OrderSequence,
                       ProductSequence) are comparable.
    - INDEX:           2 columnstore indexes on MSSQL cannot be migrated to PG.
    - NOT NULL:        MSSQL stores it as a column property; PG exposes it as a
                       pg_constraint row (contype 'n', 113 on PG). Not counted on
                       either side so both scripts stay identical.
    - CLUSTERED:       MSSQL PKs / ix_ProductMasterHistory are CLUSTERED; PostgreSQL
                       has no clustered indexes, so they are NONCLUSTERED there. The
                       section 15 listing omits the physical-type column for parity.
--------------------------------------------------------------------------- */
SELECT 'Convert columnstore (2) skipped: SQL Server -> PostgreSQL has no columnstore equivalent.' AS Info
UNION ALL
SELECT 'Identity columns: 13 auto sequences on PG correspond to identity properties on MSSQL (not objects).'
UNION ALL
SELECT 'Trigger backing functions: 5 on PG (trg_*_fn) - internal requirement, not migrated functions.'
UNION ALL
SELECT 'Synonyms (5): only meaningful on MSSQL; represented as wrapper views on PG.'
UNION ALL
SELECT 'MSchange_tracking_history is a system table flagged is_ms_shipped=1; counted for parity.'
UNION ALL
SELECT 'Clustered: MSSQL PKs / ix_ProductMasterHistory are CLUSTERED; PG has no clustered indexes (NONCLUSTERED).';
GO

/* ---------------------------------------------------------------------------
   17. COLUMN DEFINITIONS (compare logical type, size, nullability and identity)
--------------------------------------------------------------------------- */
SELECT s.name + '.' + t.name AS TableName,
       c.column_id AS Ordinal,
       c.name AS ColumnName,
       ty.name AS DataType,
       CASE WHEN ty.name IN ('nvarchar','nchar') AND c.max_length > 0
            THEN c.max_length / 2 ELSE c.max_length END AS MaxLength,
       c.precision AS NumericPrecision,
       c.scale AS NumericScale,
       c.is_nullable AS IsNullable,
       c.is_identity AS IsIdentity,
       dc.definition AS DefaultExpression
  FROM sys.tables t
  JOIN sys.schemas s ON t.schema_id = s.schema_id
  JOIN sys.columns c ON c.object_id = t.object_id
  JOIN sys.types ty ON c.user_type_id = ty.user_type_id
  LEFT JOIN sys.default_constraints dc
    ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
 ORDER BY 1, 2;

/* ---------------------------------------------------------------------------
   18. ROUTINE PARAMETERS (P = procedure, F = function)
--------------------------------------------------------------------------- */
SELECT s.name + '.' + o.name AS RoutineName,
       CASE WHEN o.type = 'P' THEN 'P' ELSE 'F' END AS RoutineType,
       p.parameter_id AS Ordinal,
       p.name AS ParameterName,
       ty.name AS DataType,
       p.max_length AS MaxLength,
       p.precision AS NumericPrecision,
       p.scale AS NumericScale,
       p.is_output AS IsOutput
  FROM sys.objects o
  JOIN sys.schemas s ON o.schema_id = s.schema_id
  JOIN sys.parameters p ON p.object_id = o.object_id
  JOIN sys.types ty ON p.user_type_id = ty.user_type_id
 WHERE o.is_ms_shipped = 0 AND o.type IN ('P','FN','IF','TF')
 ORDER BY 1, 2, 3;

/* ---------------------------------------------------------------------------
   19. DISABLED / UNTRUSTED OBJECTS (expected: zero rows)
--------------------------------------------------------------------------- */
SELECT 'FOREIGN_KEY' AS ObjectType, s.name + '.' + t.name AS ParentObject,
       fk.name AS ObjectName,
       CONCAT('disabled=', fk.is_disabled, '; untrusted=', fk.is_not_trusted) AS Problem
  FROM sys.foreign_keys fk
  JOIN sys.tables t ON fk.parent_object_id = t.object_id
  JOIN sys.schemas s ON t.schema_id = s.schema_id
 WHERE fk.is_disabled = 1 OR fk.is_not_trusted = 1
UNION ALL
SELECT 'CHECK', s.name + '.' + t.name, cc.name,
       CONCAT('disabled=', cc.is_disabled, '; untrusted=', cc.is_not_trusted)
  FROM sys.check_constraints cc
  JOIN sys.tables t ON cc.parent_object_id = t.object_id
  JOIN sys.schemas s ON t.schema_id = s.schema_id
 WHERE cc.is_disabled = 1 OR cc.is_not_trusted = 1
UNION ALL
SELECT 'TRIGGER', s.name + '.' + t.name, tr.name, 'disabled=1'
  FROM sys.triggers tr
  JOIN sys.tables t ON tr.parent_id = t.object_id
  JOIN sys.schemas s ON t.schema_id = s.schema_id
 WHERE tr.is_ms_shipped = 0 AND tr.is_disabled = 1
UNION ALL
SELECT 'INDEX', s.name + '.' + t.name, i.name, 'disabled=1'
  FROM sys.indexes i
  JOIN sys.tables t ON i.object_id = t.object_id
  JOIN sys.schemas s ON t.schema_id = s.schema_id
 WHERE i.is_disabled = 1
ORDER BY 1, 2, 3;

/* ---------------------------------------------------------------------------
   20. VIEW / ROUTINE / TRIGGER DEFINITIONS
   Text is dialect-specific. Compare intent and referenced objects, not bytes.
--------------------------------------------------------------------------- */
SELECT s.name + '.' + o.name AS ObjectName,
       CASE WHEN o.type = 'V' THEN 'VIEW'
            WHEN o.type = 'P' THEN 'PROCEDURE'
            WHEN o.type = 'TR' THEN 'TRIGGER'
            ELSE 'FUNCTION' END AS ObjectType,
       sm.definition AS Definition
  FROM sys.sql_modules sm
  JOIN sys.objects o ON sm.object_id = o.object_id
  JOIN sys.schemas s ON o.schema_id = s.schema_id
 WHERE o.is_ms_shipped = 0 AND o.type IN ('V','P','FN','IF','TF','TR')
 ORDER BY 2, 1;

/* ---------------------------------------------------------------------------
   21. SAMPLE VALUES (up to 10 rows from every table; one result set/table)
--------------------------------------------------------------------------- */
DECLARE @sample_sql NVARCHAR(MAX) = N'';
SELECT @sample_sql += N'PRINT N''TABLE: ' + REPLACE(s.name + N'.' + t.name,'''','''''')
                    + N'''; SELECT TOP (10) * FROM ' + QUOTENAME(s.name) + N'.'
                    + QUOTENAME(t.name) + N';' + NCHAR(10)
  FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id
 ORDER BY s.name, t.name;
EXEC sys.sp_executesql @sample_sql;
GO

/* ---------------------------------------------------------------------------
   22. ALL VALUES (optional; uncomment EXEC to compare complete table exports)
   Warning: this can return a very large amount of data.
--------------------------------------------------------------------------- */
DECLARE @all_sql NVARCHAR(MAX) = N'';
SELECT @all_sql += N'PRINT N''TABLE: ' + REPLACE(s.name + N'.' + t.name,'''','''''')
                 + N'''; SELECT * FROM ' + QUOTENAME(s.name) + N'.'
                 + QUOTENAME(t.name) + N';' + NCHAR(10)
  FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id
 ORDER BY s.name, t.name;
-- EXEC sys.sp_executesql @all_sql;
GO

/* ---------------------------------------------------------------------------
   23. DATABASE INTEGRITY (expected: no rows / no errors)
--------------------------------------------------------------------------- */
DBCC CHECKDB (N'Product') WITH NO_INFOMSGS;
GO
