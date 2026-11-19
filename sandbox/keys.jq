# Turns keys.sql's catalog facts into keys.sh's plan: which tables need a
# replication key, the SQL that adds them (on Heroku) and removes them later
# (on Heroku and Render), and preflight report lines.
#
#   jq -n --argjson src <keys.sql on Heroku> --argjson dst_added <.added on Render>
#         --argjson nulls <{table: "t"|"f"|"timeout"|"error"}> -f keys.jq
#
# Identifiers and literals in the input are already quoted by Postgres.
# Nothing volatile (row counts, sizes) goes into the SQL text: keys.sh
# compares its hash with the one the runbook showed before running it.

def cols: "(" + (.columns | join(", ")) + ")";

# Why a table needs a key, or null when Bucardo can use the one it has.
def need($nulls):
  (.key) as $k
  | if $k == null then
      (if .marked then "the replication key pg-migrate started adding has no unique index yet"
       else "no primary key or unique index" end)
    elif ($k.valid | not) then
      (if $k.marked then "the unique index on its replication key, \($k.index), is invalid (a CREATE INDEX CONCURRENTLY failed)"
       else "Bucardo would use its unique index \($k.index), which is invalid" end)
    elif $k.nullable and $nulls[.table] == "t" then
      (if $k.marked then "some rows have no \(.col) yet"
       else "Bucardo would use its unique index \($k.index), but some rows have NULL in \($k | cols)" end)
    else null end;

def add_one($i; $n):
  . as $t
  | [ "-- \($i)/\($n) \($t.table): \($t.reason)",
      "SET lock_timeout = '5s';",
      "CREATE SEQUENCE IF NOT EXISTS \($t.seq);",
      "ALTER TABLE \($t.table) ADD COLUMN IF NOT EXISTS \($t.col) bigint;",
      "ALTER TABLE \($t.table) ALTER COLUMN \($t.col) SET DEFAULT nextval(\($t.seq_lit)::regclass);",
      "ALTER SEQUENCE \($t.seq) OWNED BY \($t.table).\($t.col);",
      "COMMENT ON COLUMN \($t.table).\($t.col) IS 'pg-migrate replication key: lets Bucardo replicate this table. Safe to drop once the migration is done.';",
      "RESET lock_timeout;",
      "DO $pgm$",
      "DECLARE",
      "  -- Fill \($t.col) on existing rows: one pass over the table's pages, 100 per",
      "  -- transaction. New rows get it from the column default.",
      "  pages    bigint := pg_relation_size(\($t.table_lit)) / current_setting('block_size')::bigint;",
      "  page     bigint := 0;",
      "  n        bigint;",
      "  filled   bigint := 0;",
      "  reported timestamptz := clock_timestamp();",
      "BEGIN",
      "  WHILE page < pages LOOP",
      "    UPDATE \($t.table) SET \($t.col) = nextval(\($t.seq_lit)::regclass)",
      "     WHERE ctid >= format('(%s,0)', page)::tid AND ctid < format('(%s,0)', page + 100)::tid",
      "       AND \($t.col) IS NULL;",
      "    GET DIAGNOSTICS n = ROW_COUNT;",
      "    filled := filled + n;",
      "    page := page + 100;",
      "    COMMIT;",
      "    IF clock_timestamp() - reported > interval '10 seconds' THEN",
      "      RAISE NOTICE '%: % of % pages, % rows filled', \($t.table_lit), least(page, pages), pages, filled;",
      "      reported := clock_timestamp();",
      "    END IF;",
      "  END LOOP;",
      "  -- Rows the app updated meanwhile can land behind the pass: fill those too.",
      "  LOOP",
      "    UPDATE \($t.table) SET \($t.col) = nextval(\($t.seq_lit)::regclass)",
      "     WHERE ctid IN (SELECT ctid FROM \($t.table) WHERE \($t.col) IS NULL LIMIT 10000);",
      "    GET DIAGNOSTICS n = ROW_COUNT;",
      "    EXIT WHEN n = 0;",
      "    filled := filled + n;",
      "    COMMIT;",
      "  END LOOP;",
      "  RAISE NOTICE '%: % row(s) filled', \($t.table_lit), filled;",
      "END",
      "$pgm$;" ]
    + (if $t.idx_state == "invalid" then
         ["-- An earlier CREATE INDEX CONCURRENTLY failed and left this index invalid.",
          "DROP INDEX CONCURRENTLY IF EXISTS \($t.idx_q);"]
       else [] end)
    + [ "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS \($t.idx) ON \($t.table) (\($t.col));",
        "VACUUM (ANALYZE) \($t.table);" ]
  | join("\n");

def drop_one:
  [ "-- \(.table)" ]
  + [ .indexes[] | "DROP INDEX CONCURRENTLY IF EXISTS \(.);" ]
  + [ "SET lock_timeout = '5s';",
      "ALTER TABLE IF EXISTS \(.table) DROP COLUMN IF EXISTS \(.col);",
      "RESET lock_timeout;" ]
  | join("\n");

($src.tables | map(. + {reason: need($nulls)})) as $tables
| [ $tables[] | select(.reason != null) ] as $needs
| [ $needs[] | select(.manual == null) ] as $auto
| [ $needs[] | select(.manual != null) ] as $manual
| [ $tables[] | select(.reason == null and .key != null) ] as $keyed
| ([ $src.added[], $dst_added[] ]
   | group_by([.table, .col])
   | map(.[0] + {indexes: ([ .[].indexes[] ] | unique)})) as $added
| {
    tables: [ $needs[] | {table, reason, manual, owner_ok, rows, size, update_triggers,
                          col, sequence: .seq, index: .idx_q} ],
    keyed: [ $keyed[] | {table, index: .key.index, columns: .key.columns, nullable: .key.nullable,
                         added: .key.marked, nulls: $nulls[.table]} ],
    added: $added,
    add_sql: (if $auto == [] then "" else
      ([ "-- pg-migrate: add a replication key to each table Bucardo can't replicate yet:",
         "-- a bigint column, filled in batches, with a unique index built without",
         "-- locking the table. Run it on Heroku with psql (heroku pg:psql -a <app>),",
         "-- not inside a transaction: the backfill and CREATE INDEX CONCURRENTLY commit",
         "-- as they go. Safe to run again. Then run preflight again.",
         "SET statement_timeout = 0;" ] | join("\n"))
      + "\n\n"
      + ([ $auto | length as $n | to_entries[] | (.key + 1) as $i | .value | add_one($i; $n) ] | join("\n\n"))
      + "\n"
      end),
    drop_sql: (if $added == [] then "" else
      ([ "-- pg-migrate: remove the replication keys pg-migrate added. Run it after cutover",
         "-- and Clean up Heroku: Bucardo's triggers on Heroku use these columns, and",
         "-- writes to the table fail if the column goes first. Run it on Heroku and on",
         "-- Render with psql, not inside a transaction. Safe to run again. Dropping a",
         "-- column also drops the sequence it owns.",
         "SET statement_timeout = 0;" ] | join("\n"))
      + "\n\n" + ([ $added[] | drop_one ] | join("\n\n")) + "\n"
      end),
    report: (
      (if $needs == [] then
         (if $keyed == [] then [{level: "INFO", text: "every table has a primary key"}]
          else [{level: "INFO", text: "every table has a primary key or a unique index Bucardo can use as its key"}] end)
       else
         [{level: "BLOCKER",
           text: ("tables without a primary key (Bucardo cannot replicate them): "
                  + ([ $needs[].table ] | join(", ")) + ". "
                  + (if $manual == [] then "The runbook's Replication keys step has SQL that adds one to each"
                     elif $auto == [] then "Add a primary key or unique index by hand (see the warnings)"
                     else "The runbook's Replication keys step has SQL that adds one to each except "
                       + ([ $manual[].table ] | join(", ")) + ", which need one added by hand (see the warnings)"
                     end))}]
       end)
      + [ $needs[] | select(.reason != "no primary key or unique index")
          | {level: "WARN", text: "\(.table): \(.reason)"} ]
      + [ $manual[] | {level: "WARN", text: "\(.table): no SQL generated, because \(.manual)"} ]
      + [ $auto[] | select(.owner_ok | not)
          | {level: "WARN", text: "\(.table): the Heroku role doesn't own it or can't create objects in its schema, so its owner must run the SQL"} ]
      + [ $auto[] | select(.update_triggers != [])
          | {level: "WARN", text: "\(.table): the backfill updates every row once, which fires its UPDATE trigger(s) \(.update_triggers | join(", ")) for each row. Check that they don't change data (an updated_at column, an audit log)"} ]
      + [ $keyed[] | {level: "INFO", text: ("\(.table) has no primary key; Bucardo will use "
            + (if .key.marked then "the replication key pg-migrate added, " else "its unique index " end)
            + "\(.key.index) \(.key | cols)")} ]
      + [ $keyed[] | select(.key.nullable and (.key.marked | not) and $nulls[.table] == "f")
          | {level: "WARN", text: "\(.table): the columns of \(.key.index) allow NULL. None has one now, but a row that gets one later won't replicate its updates and deletes (cutover's row counts would catch it)"} ]
      + [ $keyed[] | select(.key.nullable and ($nulls[.table] == "timeout" or $nulls[.table] == "error"))
          | {level: "WARN", text: "\(.table): couldn't check whether \(.key | cols) of \(.key.index) holds NULLs (\(if $nulls[.table] == "timeout" then "timed out after 30s" else "query failed" end)). Rows with a NULL there won't replicate their updates and deletes"} ]
    )
  }
