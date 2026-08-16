-- =============================================================================
--  MIRRORED VERIFICATION SCRIPT - POSTGRESQL (product)
-- =============================================================================
-- Run this against the PostgreSQL target and verify_mssql.sql against the MSSQL
-- source/target. Every section below is written so that the numbers and rows it
-- returns MATCH the corresponding section of verify_mssql.sql 1:1.
--
-- Usage:
--     psql -h host -p 5432 -U user -d product -f scripts/verify_postgres.sql
--   (section 8 needs psql because it uses the \gexec meta-command; every other
--    section runs in any SQL client)
--
-- Notable differences vs. earlier ad-hoc scripts (these are INTENTIONAL so the
-- two scripts produce identical output):
--   * VIEW counts all views. The 5 MSSQL synonyms were migrated as wrapper
--     views, so MSSQL (3 views + 5 synonyms) == PG (8 views).
--   * SQL_SCALAR_FUNCTION excludes the 5 trigger-backing functions (trg_*_fn)
--     that PostgreSQL requires for triggers; MSSQL does not have these.
--   * SEQUENCE_OBJECT only counts standalone sequences; the 13 *_seq sequences
--     auto-created for identity columns are excluded (MSSQL hides these inside
--     the identity column property).
--   * INDEX counts valid, non-PK, non-unique indexes. Columnstore indexes from
--     MSSQL were not migrated.
--   * Result column names are double-quoted so psql shows the same headers as
--     the MSSQL script (psql lower-cases unquoted identifiers).
-- =============================================================================

\pset pager off
\pset null '<NULL>'
SET search_path = dbo;

-- ---------------------------------------------------------------------------
-- 1. OBJECT INVENTORY SUMMARY (counts per category - must match MSSQL exactly)
-- ---------------------------------------------------------------------------
SELECT 'USER_TABLE'              AS "ObjectType", COUNT(*) AS "ObjectCount"
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'r' AND n.nspname = 'dbo'
UNION ALL
SELECT 'VIEW', COUNT(*)
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'v' AND n.nspname = 'dbo'
UNION ALL
SELECT 'SQL_SCALAR_FUNCTION', COUNT(*)
  FROM pg_proc p
  JOIN pg_namespace n ON p.pronamespace = n.oid
 WHERE p.prokind = 'f' AND n.nspname = 'dbo'
   AND NOT EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgfoid = p.oid)
UNION ALL
SELECT 'SQL_STORED_PROCEDURE', COUNT(*)
  FROM pg_proc p
  JOIN pg_namespace n ON p.pronamespace = n.oid
 WHERE p.prokind = 'p' AND n.nspname = 'dbo'
UNION ALL
SELECT 'SQL_TRIGGER', COUNT(*)
  FROM pg_trigger t
  JOIN pg_class c ON t.tgrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE NOT t.tgisinternal AND n.nspname = 'dbo'
UNION ALL
SELECT 'SEQUENCE_OBJECT', COUNT(*)
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'S' AND n.nspname = 'dbo'
   AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
         WHERE d.objid = c.oid
           AND d.classid = 'pg_class'::regclass
           AND d.refclassid = 'pg_class'::regclass
           AND d.refobjsubid > 0)
UNION ALL
SELECT 'PRIMARY_KEY_CONSTRAINT', COUNT(*)
  FROM pg_constraint con
  JOIN pg_class c ON con.conrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE con.contype = 'p' AND n.nspname = 'dbo'
UNION ALL
SELECT 'UNIQUE_CONSTRAINT', COUNT(*)
  FROM pg_constraint con
  JOIN pg_class c ON con.conrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE con.contype = 'u' AND n.nspname = 'dbo'
UNION ALL
SELECT 'FOREIGN_KEY_CONSTRAINT', COUNT(*)
  FROM pg_constraint con
  JOIN pg_class c ON con.conrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE con.contype = 'f' AND n.nspname = 'dbo'
UNION ALL
SELECT 'CHECK_CONSTRAINT', COUNT(*)
  FROM pg_constraint con
  JOIN pg_class c ON con.conrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE con.contype = 'c' AND n.nspname = 'dbo'
UNION ALL
SELECT 'DEFAULT_CONSTRAINT', COUNT(*)
  FROM pg_attrdef ad
  JOIN pg_class c ON ad.adrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE n.nspname = 'dbo'
UNION ALL
-- Non-unique, non-PK indexes (the 2 MSSQL columnstore indexes were not migrated)
SELECT 'INDEX', COUNT(*)
  FROM pg_index idx
  JOIN pg_class c ON idx.indrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE n.nspname = 'dbo' AND idx.indisvalid
   AND NOT idx.indisprimary AND NOT idx.indisunique
UNION ALL
SELECT 'UNIQUE_INDEX', COUNT(*)
  FROM pg_index idx
  JOIN pg_class c ON idx.indrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE n.nspname = 'dbo' AND idx.indisvalid
   AND idx.indisunique AND NOT idx.indisprimary
UNION ALL
SELECT 'PK_INDEX', COUNT(*)
  FROM pg_index idx
  JOIN pg_class c ON idx.indrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE n.nspname = 'dbo' AND idx.indisvalid AND idx.indisprimary
ORDER BY "ObjectType";

-- ---------------------------------------------------------------------------
-- 2. TABLES
-- ---------------------------------------------------------------------------
SELECT n.nspname AS "SchemaName", c.relname AS "TableName"
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'r' AND n.nspname = 'dbo'
 ORDER BY 1, 2;

-- ---------------------------------------------------------------------------
-- 3. VIEWS (includes the 5 wrapper views that represent MSSQL synonyms)
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname AS "ObjectName"
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'v' AND n.nspname = 'dbo'
 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 4. SCALAR FUNCTIONS (excludes the 5 trigger-backing trg_*_fn functions)
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || p.proname AS "FunctionName"
  FROM pg_proc p
  JOIN pg_namespace n ON p.pronamespace = n.oid
 WHERE p.prokind = 'f' AND n.nspname = 'dbo'
   AND NOT EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgfoid = p.oid)
 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 5. STORED PROCEDURES
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || p.proname AS "ProcedureName"
  FROM pg_proc p
  JOIN pg_namespace n ON p.pronamespace = n.oid
 WHERE p.prokind = 'p' AND n.nspname = 'dbo'
 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 6. TRIGGERS
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || lower(t.tgname) AS "TriggerName",
       n.nspname || '.' || c.relname AS "OnTable"
  FROM pg_trigger t
  JOIN pg_class c ON t.tgrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE NOT t.tgisinternal AND n.nspname = 'dbo'
 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 7. SEQUENCES (standalone only; *_seq identity sequences excluded)
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname AS "SequenceName"
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'S' AND n.nspname = 'dbo'
   AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
         WHERE d.objid = c.oid
           AND d.classid = 'pg_class'::regclass
           AND d.refclassid = 'pg_class'::regclass
           AND d.refobjsubid > 0)
 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 8. ROW COUNTS - per table (exact) and TOTAL
--    The query below BUILDS one UNION ALL of COUNT(*) per table plus a final
--    TOTAL row, then runs it with \gexec. (Requires psql.)
-- ---------------------------------------------------------------------------
SELECT
    string_agg(
        'SELECT ' || quote_literal(n.nspname || '.' || c.relname)
                 || ' AS "TableName", COUNT(*) AS "RowCount" FROM '
                 || quote_ident(n.nspname) || '.' || quote_ident(c.relname),
        E'\nUNION ALL\n' ORDER BY n.nspname, c.relname)
    || E'\nUNION ALL\n'
    || 'SELECT ''TOTAL'', SUM("RowCount") FROM ('
    || string_agg(
        'SELECT COUNT(*) AS "RowCount" FROM '
        || quote_ident(n.nspname) || '.' || quote_ident(c.relname),
        E'\nUNION ALL\n' ORDER BY n.nspname, c.relname)
    || ') AS T'
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'r' AND n.nspname = 'dbo' \gexec

-- ---------------------------------------------------------------------------
-- 9. COLUMN COUNT PER TABLE
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname AS "TableName", COUNT(*) AS "ColumnCount"
  FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
  JOIN pg_attribute a ON a.attrelid = c.oid
 WHERE c.relkind = 'r' AND n.nspname = 'dbo'
   AND a.attnum > 0 AND NOT a.attisdropped
 GROUP BY 1
 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 10. PRIMARY KEYS
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || t.relname AS "TableName",
       con.conname                   AS "ConstraintName",
       a.attname                     AS "ColumnName",
       k.ord                         AS "Ordinal"
  FROM pg_constraint con
  JOIN pg_class t ON con.conrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
  JOIN LATERAL unnest(con.conkey) WITH ORDINALITY k(attnum, ord) ON true
  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
 WHERE con.contype = 'p' AND n.nspname = 'dbo'
 ORDER BY 1, 4;

-- ---------------------------------------------------------------------------
-- 11. UNIQUE CONSTRAINTS
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || t.relname AS "TableName",
       con.conname                   AS "ConstraintName",
       a.attname                     AS "ColumnName",
       k.ord                         AS "Ordinal"
  FROM pg_constraint con
  JOIN pg_class t ON con.conrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
  JOIN LATERAL unnest(con.conkey) WITH ORDINALITY k(attnum, ord) ON true
  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
 WHERE con.contype = 'u' AND n.nspname = 'dbo'
 ORDER BY 1, 4;

-- ---------------------------------------------------------------------------
-- 12. CHECK CONSTRAINTS
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || t.relname AS "TableName",
       con.conname                   AS "ConstraintName",
       pg_get_constraintdef(con.oid) AS "Definition"
  FROM pg_constraint con
  JOIN pg_class t ON con.conrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
 WHERE con.contype = 'c' AND n.nspname = 'dbo'
 ORDER BY 1, 2;

-- ---------------------------------------------------------------------------
-- 13. DEFAULT CONSTRAINTS
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || t.relname AS "TableName",
       a.attname                     AS "ColumnName",
       pg_get_expr(ad.adbin, ad.adrelid) AS "Definition"
  FROM pg_attrdef ad
  JOIN pg_class t ON ad.adrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
  JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ad.adnum
 WHERE n.nspname = 'dbo'
 ORDER BY 1, 2;

-- ---------------------------------------------------------------------------
-- 14. FOREIGN KEYS
-- ---------------------------------------------------------------------------
SELECT con.conname AS "FKName",
       n1.nspname || '.' || t1.relname AS "ReferencingTable",
       a1.attname   AS "ReferencingColumn",
       n2.nspname || '.' || t2.relname AS "ReferencedTable",
       a2.attname   AS "ReferencedColumn"
  FROM pg_constraint con
  JOIN pg_class t1 ON con.conrelid = t1.oid
  JOIN pg_namespace n1 ON t1.relnamespace = n1.oid
  JOIN pg_class t2 ON con.confrelid = t2.oid
  JOIN pg_namespace n2 ON t2.relnamespace = n2.oid
  JOIN LATERAL unnest(con.conkey)  WITH ORDINALITY k1(attnum, ord) ON true
  JOIN LATERAL unnest(con.confkey) WITH ORDINALITY k2(attnum, ord) ON true AND k2.ord = k1.ord
  JOIN pg_attribute a1 ON a1.attrelid = con.conrelid   AND a1.attnum = k1.attnum
  JOIN pg_attribute a2 ON a2.attrelid = con.confrelid  AND a2.attnum = k2.attnum
 WHERE con.contype = 'f' AND n1.nspname = 'dbo'
 ORDER BY 1;

-- ---------------------------------------------------------------------------
-- 15. INDEXES (all, including PK/unique; no columnstore equivalent on PG)
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || t.relname AS "TableName",
       i.relname                     AS "IndexName",
       CASE WHEN idx.indisprimary THEN 'PK_INDEX'
            WHEN idx.indisunique  THEN 'UNIQUE_INDEX'
            ELSE 'INDEX' END        AS "IndexType",
       a.attname                     AS "ColumnName"
  FROM pg_index idx
  JOIN pg_class t ON idx.indrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
  JOIN pg_class i ON idx.indexrelid = i.oid
  JOIN LATERAL unnest(idx.indkey) WITH ORDINALITY k(attnum, ord) ON true
  JOIN pg_attribute a ON a.attrelid = idx.indrelid AND a.attnum = k.attnum
 WHERE idx.indisvalid AND n.nspname = 'dbo' AND k.attnum <> 0
 ORDER BY 1, 2, 4;

-- ---------------------------------------------------------------------------
-- 16. WHY SOME CATEGORIES DIFFER FROM RAW pg_catalog (informational)
--     These rows are for your information only; the matching queries above
--     already normalise them so BOTH databases report the same numbers.
-- ---------------------------------------------------------------------------
SELECT 'Convert columnstore (2) skipped: SQL Server -> PostgreSQL has no columnstore equivalent.' AS "Info"
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

-- ---------------------------------------------------------------------------
-- 17. COLUMN DEFINITIONS (compare logical type, size, nullability and identity)
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || t.relname AS "TableName",
       a.attnum AS "Ordinal",
       a.attname AS "ColumnName",
       format_type(a.atttypid, NULL) AS "DataType",
       CASE WHEN a.atttypmod > 4 AND a.atttypid IN ('varchar'::regtype,'bpchar'::regtype)
            THEN a.atttypmod - 4 ELSE NULL END AS "MaxLength",
       CASE WHEN a.atttypid = 'numeric'::regtype AND a.atttypmod >= 4
            THEN ((a.atttypmod - 4) >> 16) & 65535 ELSE NULL END AS "NumericPrecision",
       CASE WHEN a.atttypid = 'numeric'::regtype AND a.atttypmod >= 4
            THEN (a.atttypmod - 4) & 65535 ELSE NULL END AS "NumericScale",
       NOT a.attnotnull AS "IsNullable",
       (a.attidentity <> '') AS "IsIdentity",
       pg_get_expr(ad.adbin, ad.adrelid) AS "DefaultExpression"
  FROM pg_class t
  JOIN pg_namespace n ON t.relnamespace = n.oid
  JOIN pg_attribute a ON a.attrelid = t.oid
  LEFT JOIN pg_attrdef ad ON ad.adrelid = t.oid AND ad.adnum = a.attnum
 WHERE t.relkind = 'r' AND n.nspname = 'dbo'
   AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY 1, 2;

-- ---------------------------------------------------------------------------
-- 18. ROUTINE PARAMETERS (P = procedure, F = function)
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || p.proname AS "RoutineName",
       CASE WHEN p.prokind = 'p' THEN 'P' ELSE 'F' END AS "RoutineType",
       x.ord AS "Ordinal",
       COALESCE(p.proargnames[x.ord], '') AS "ParameterName",
       format_type(x.type_oid, NULL) AS "DataType",
       NULL::integer AS "MaxLength",
       NULL::integer AS "NumericPrecision",
       NULL::integer AS "NumericScale",
       COALESCE(p.proargmodes[x.ord] IN ('o','b','t'), false) AS "IsOutput"
  FROM pg_proc p
  JOIN pg_namespace n ON p.pronamespace = n.oid
  CROSS JOIN LATERAL unnest(COALESCE(p.proallargtypes, p.proargtypes::oid[]))
       WITH ORDINALITY x(type_oid, ord)
 WHERE p.prokind IN ('p','f') AND n.nspname = 'dbo'
   AND NOT EXISTS (SELECT 1 FROM pg_trigger tr WHERE tr.tgfoid = p.oid)
 ORDER BY 1, 2, 3;

-- ---------------------------------------------------------------------------
-- 19. DISABLED / UNVALIDATED OBJECTS (expected: zero rows)
-- PostgreSQL cannot disable FK/CHECK constraints or indexes in SQL Server's
-- sense; NOT VALID and invalid indexes are the corresponding problem states.
-- ---------------------------------------------------------------------------
SELECT CASE con.contype WHEN 'f' THEN 'FOREIGN_KEY' ELSE 'CHECK' END AS "ObjectType",
       n.nspname || '.' || t.relname AS "ParentObject", con.conname AS "ObjectName",
       'validated=false' AS "Problem"
  FROM pg_constraint con
  JOIN pg_class t ON con.conrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
 WHERE con.contype IN ('f','c') AND NOT con.convalidated AND n.nspname = 'dbo'
UNION ALL
SELECT 'TRIGGER', n.nspname || '.' || t.relname, tr.tgname, 'disabled=1'
  FROM pg_trigger tr
  JOIN pg_class t ON tr.tgrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
 WHERE NOT tr.tgisinternal AND tr.tgenabled = 'D' AND n.nspname = 'dbo'
UNION ALL
SELECT 'INDEX', n.nspname || '.' || t.relname, i.relname, 'valid=false'
  FROM pg_index ix
  JOIN pg_class t ON ix.indrelid = t.oid
  JOIN pg_class i ON ix.indexrelid = i.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
 WHERE NOT ix.indisvalid AND n.nspname = 'dbo'
ORDER BY 1, 2, 3;

-- ---------------------------------------------------------------------------
-- 20. VIEW / ROUTINE / TRIGGER DEFINITIONS
-- Text is dialect-specific. Compare intent and referenced objects, not bytes.
-- ---------------------------------------------------------------------------
SELECT n.nspname || '.' || c.relname AS "ObjectName", 'VIEW' AS "ObjectType",
       pg_get_viewdef(c.oid, true) AS "Definition"
  FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'v' AND n.nspname = 'dbo'
UNION ALL
SELECT n.nspname || '.' || p.proname,
       CASE WHEN p.prokind = 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
       pg_get_functiondef(p.oid)
  FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
 WHERE p.prokind IN ('p','f') AND n.nspname = 'dbo'
   AND NOT EXISTS (SELECT 1 FROM pg_trigger tr WHERE tr.tgfoid = p.oid)
UNION ALL
SELECT n.nspname || '.' || tr.tgname, 'TRIGGER', pg_get_triggerdef(tr.oid, true)
  FROM pg_trigger tr
  JOIN pg_class c ON tr.tgrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE NOT tr.tgisinternal AND n.nspname = 'dbo'
ORDER BY 2, 1;

-- ---------------------------------------------------------------------------
-- 21. SAMPLE VALUES (up to 10 rows from every table; one result set/table)
-- Requires psql because \gexec executes the generated SELECT statements.
-- ---------------------------------------------------------------------------
SELECT format('SELECT %L AS "SourceTable", q.* FROM (SELECT * FROM %I.%I LIMIT 10) q;',
              n.nspname || '.' || c.relname, n.nspname, c.relname)
  FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'r' AND n.nspname = 'dbo'
 ORDER BY n.nspname, c.relname \gexec

-- ---------------------------------------------------------------------------
-- 22. ALL VALUES (optional; remove the leading -- before \gexec to execute)
-- Warning: this can return a very large amount of data.
-- ---------------------------------------------------------------------------
SELECT format('SELECT %L AS "SourceTable", q.* FROM %I.%I q;',
              n.nspname || '.' || c.relname, n.nspname, c.relname)
  FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
 WHERE c.relkind = 'r' AND n.nspname = 'dbo'
 ORDER BY n.nspname, c.relname;
-- \gexec

-- ---------------------------------------------------------------------------
-- 23. DATABASE INTEGRITY
-- PostgreSQL has no online SQL equivalent to DBCC CHECKDB. pg_amcheck is the
-- closest physical-corruption check and must be run from the operating system:
--     pg_amcheck --database=product --schema=dbo --verbose
-- This SQL check reports invalid indexes and unvalidated constraints.
-- ---------------------------------------------------------------------------
SELECT 'INVALID_INDEX' AS "ProblemType", n.nspname || '.' || i.relname AS "ObjectName"
  FROM pg_index ix
  JOIN pg_class i ON ix.indexrelid = i.oid
  JOIN pg_namespace n ON i.relnamespace = n.oid
 WHERE NOT ix.indisvalid AND n.nspname = 'dbo'
UNION ALL
SELECT 'UNVALIDATED_CONSTRAINT', n.nspname || '.' || con.conname
  FROM pg_constraint con
  JOIN pg_class t ON con.conrelid = t.oid
  JOIN pg_namespace n ON t.relnamespace = n.oid
 WHERE NOT con.convalidated AND n.nspname = 'dbo';
