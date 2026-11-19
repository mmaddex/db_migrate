-- Catalog facts for keys.sh, as one JSON object. Read-only.
--
--   psql -v schemas="ARRAY['public']::text[]" -v marker='pg-migrate replication key' -f keys.sql
--
-- "tables": every table in the schemas without a primary key, with the
-- unique index Bucardo would use as its key instead. Bucardo picks it in
-- bucardo.validate_goat (bucardo.schema): a unique index with no predicate
-- and no expressions, the one with the fewest columns, the newest on a tie.
-- Bucardo doesn't check that the index is valid or its columns NOT NULL,
-- so these facts include both, and keys.sh decides.
--
-- For each table it also names the replication key pg-migrate would add:
-- a bigint column (mig_id, or the column marked with the comment if an
-- earlier run added one), its sequence and its unique index, and whether
-- each already exists. Identifiers come quoted (quote_ident), literals
-- quoted (quote_literal), so keys.jq can put them in SQL as they are.
--
-- "added": every column pg-migrate added (found by its comment), with the
-- indexes on exactly that column.
SELECT json_build_object(
  'tables', (
    SELECT coalesce(json_agg(row_to_json(x) ORDER BY x.sort), '[]')
    FROM (
      SELECT
        n.nspname || '.' || c.relname AS sort,
        quote_ident(n.nspname) || '.' || quote_ident(c.relname) AS "table",
        quote_literal(quote_ident(n.nspname) || '.' || quote_ident(c.relname)) AS table_lit,
        CASE WHEN c.relkind = 'p' THEN 'it is partitioned: give the parent a primary key or unique index that includes the partition key'
             WHEN c.relispartition THEN 'it is a partition: give its partitioned parent a key'
             WHEN c.relhassubclass OR EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = c.oid)
               THEN 'it uses table inheritance'
             WHEN nm.col IS NULL THEN 'it already has columns mig_id to mig_id_9'
             WHEN nm.seq_state = 'conflict' THEN 'a relation named ' || nm.seq_q || ' already exists'
             WHEN nm.idx_state = 'conflict' THEN 'a relation named ' || nm.idx_q || ' already exists'
        END AS manual,
        pg_has_role(c.relowner, 'USAGE') AND has_schema_privilege(n.oid, 'CREATE') AS owner_ok,
        greatest(c.reltuples, 0)::bigint AS rows,
        pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
        -- Row triggers that the backfill's UPDATE would fire.
        (SELECT coalesce(json_agg(quote_ident(tg.tgname) ORDER BY tg.tgname), '[]')
         FROM pg_trigger tg
         WHERE tg.tgrelid = c.oid AND NOT tg.tgisinternal AND tg.tgenabled <> 'D'
           AND tg.tgname NOT LIKE 'bucardo%'
           AND tg.tgtype & 1 = 1 AND tg.tgtype & 16 = 16
           AND coalesce(array_length(tg.tgattr::int2[], 1), 0) = 0) AS update_triggers,
        k.key,
        nm.marked,
        quote_ident(nm.col) AS col,
        nm.seq_q AS seq,
        quote_literal(nm.seq_q) AS seq_lit,
        quote_ident(nm.idx) AS idx,
        nm.idx_q,
        nm.idx_state
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      -- Bucardo's choice of key.
      LEFT JOIN LATERAL (
        SELECT json_build_object(
                 'index', quote_ident(n.nspname) || '.' || quote_ident(ic.relname),
                 'valid', i.indisvalid,
                 'columns', (SELECT json_agg(quote_ident(a.attname) ORDER BY u.o)
                             FROM unnest(i.indkey::int2[]) WITH ORDINALITY u(attnum, o)
                             JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = u.attnum),
                 'nullable', (SELECT bool_or(NOT a.attnotnull)
                              FROM unnest(i.indkey::int2[]) u(attnum)
                              JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = u.attnum),
                 'marked', coalesce(i.indnatts = 1 AND col_description(c.oid, i.indkey[0]) LIKE :'marker' || '%', false)
               ) AS key
        FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
        WHERE i.indrelid = c.oid AND i.indisunique AND i.indpred IS NULL AND i.indexprs IS NULL
        ORDER BY i.indnatts, i.indexrelid DESC
        LIMIT 1) k ON true
      -- The key pg-migrate adds: its column (an earlier run's, else the first
      -- free name), and names for its sequence and index that fit in 63 bytes.
      CROSS JOIN LATERAL (
        SELECT coalesce(
          (SELECT a.attname FROM pg_attribute a
           WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
             AND col_description(c.oid, a.attnum) LIKE :'marker' || '%'
           ORDER BY a.attnum LIMIT 1),
          (SELECT cand FROM generate_series(0, 9) g,
                  LATERAL (SELECT 'mig_id' || CASE WHEN g = 0 THEN '' ELSE '_' || g END AS cand) x
           WHERE NOT EXISTS (SELECT 1 FROM pg_attribute a
                             WHERE a.attrelid = c.oid AND a.attname = x.cand AND NOT a.attisdropped)
           ORDER BY g LIMIT 1)) AS col,
          EXISTS (SELECT 1 FROM pg_attribute a
                  WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                    AND col_description(c.oid, a.attnum) LIKE :'marker' || '%') AS marked
      ) mc
      CROSS JOIN LATERAL (
        SELECT CASE WHEN octet_length(c.relname) + octet_length(mc.col) + 5 <= 63 THEN c.relname
                    ELSE (SELECT left(c.relname, w) FROM generate_series(char_length(c.relname), 1, -1) w
                          WHERE octet_length(left(c.relname, w)) + 9 + octet_length(mc.col) + 5 <= 63
                          ORDER BY w DESC LIMIT 1) || '_' || left(md5(c.relname), 8)
               END AS base
      ) b
      CROSS JOIN LATERAL (
        SELECT mc.col, mc.marked,
               b.base || '_' || mc.col || '_seq' AS seq,
               b.base || '_' || mc.col || '_key' AS idx,
               quote_ident(n.nspname) || '.' || quote_ident(b.base || '_' || mc.col || '_seq') AS seq_q,
               quote_ident(n.nspname) || '.' || quote_ident(b.base || '_' || mc.col || '_key') AS idx_q,
               -- A sequence by that name is ours if nothing else owns it.
               (SELECT CASE WHEN s.relkind <> 'S' THEN 'conflict'
                            WHEN EXISTS (SELECT 1 FROM pg_depend d
                                         WHERE d.classid = 'pg_class'::regclass AND d.objid = s.oid
                                           AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
                                           AND NOT (d.refobjid = c.oid AND d.refobjsubid = (
                                             SELECT a.attnum FROM pg_attribute a
                                             WHERE a.attrelid = c.oid AND a.attname = mc.col AND NOT a.attisdropped)))
                              THEN 'conflict'
                            ELSE 'exists' END
                FROM pg_class s WHERE s.relnamespace = n.oid AND s.relname = b.base || '_' || mc.col || '_seq'
               ) AS seq_state,
               -- An index by that name is ours if it's a unique index on exactly our column.
               (SELECT CASE WHEN ix.relkind <> 'i' OR i.indrelid IS DISTINCT FROM c.oid OR NOT i.indisunique
                              OR i.indpred IS NOT NULL OR i.indexprs IS NOT NULL
                              OR i.indnatts <> 1
                              OR i.indkey[0] IS DISTINCT FROM (SELECT a.attnum FROM pg_attribute a
                                                               WHERE a.attrelid = c.oid AND a.attname = mc.col
                                                                 AND NOT a.attisdropped)
                              THEN 'conflict'
                            WHEN i.indisvalid THEN 'valid'
                            ELSE 'invalid' END
                FROM pg_class ix LEFT JOIN pg_index i ON i.indexrelid = ix.oid
                WHERE ix.relnamespace = n.oid AND ix.relname = b.base || '_' || mc.col || '_key'
               ) AS idx_state
      ) nm
      WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY (:schemas)
        AND NOT EXISTS (SELECT 1 FROM pg_constraint p WHERE p.conrelid = c.oid AND p.contype = 'p')
    ) x
  ),
  'added', (
    SELECT coalesce(json_agg(json_build_object(
             'table', quote_ident(n.nspname) || '.' || quote_ident(c.relname),
             'col', quote_ident(a.attname),
             'indexes', (SELECT coalesce(json_agg(quote_ident(n.nspname) || '.' || quote_ident(ic.relname)
                                                  ORDER BY ic.relname), '[]')
                         FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
                         WHERE i.indrelid = c.oid AND i.indnatts = 1 AND i.indkey[0] = a.attnum))
           ORDER BY n.nspname, c.relname, a.attname), '[]')
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p') AND n.nspname = ANY (:schemas)
      AND col_description(c.oid, a.attnum) LIKE :'marker' || '%'
  )
);
