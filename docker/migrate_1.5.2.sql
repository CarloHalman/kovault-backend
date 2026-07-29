-- Kovault 1.5.2 migration — apply to an EXISTING database. Idempotent: safe to run more than once.
-- No schema change, no data change: it redefines one function.
--
--   psql "$KOVAULT_DSN" -f migrate_1.5.2.sql
--   (or: docker compose exec kovault-db psql -U kovault -d kovault -f /path/migrate_1.5.2.sql)
--
-- WHY YOU WANT THIS: f_unaccent() shipped without a pinned search_path. pg_restore runs with an
-- empty search_path, so the function body could resolve neither unaccent() nor the 'unaccent'
-- regdictionary, and every table carrying a *_norm generated column (pages, headers, tasks,
-- decisions, sources) failed to create. The practical effect was that a Kovault backup could not
-- be restored with a plain pg_restore — the dump was fine, the restore collapsed.
--
-- Running this fixes the function in place, so every dump you take FROM NOW ON restores cleanly.
--
-- IT DOES NOT REPAIR DUMPS YOU ALREADY HAVE. pg_dump writes the function definition as it stood in
-- the source database, so an older dump still carries the unpinned version and still fails on
-- restore. To restore one of those, create the target database, run THIS file against it first,
-- then pg_restore into it: the dump's own CREATE FUNCTION fails with "already exists", pg_restore
-- carries on, and the fixed function is the one the generated columns use. See README, Upgrading.

BEGIN;

-- Created if absent so this file also works on an EMPTY database, which is what the
-- restore-an-old-dump recipe above needs. On a live install it is already there and this is a
-- no-op that leaves the existing schema placement alone.
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Resolved dynamically rather than written as public.unaccent(...) so it also holds when the
-- extension lives in a non-public schema. Mirrors the block in 01-schema.sql — keep them in step.
DO $do$
DECLARE ext_schema text;
BEGIN
  SELECT n.nspname INTO ext_schema
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
   WHERE e.extname = 'unaccent';
  EXECUTE format(
    'CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text
       LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path = %I, pg_catalog
       AS $f$ SELECT unaccent(''unaccent'', $1) $f$', ext_schema);
END $do$;

-- Fails the migration rather than reporting success on a function that still cannot resolve
-- unaccent under the empty search_path pg_restore uses. f_unaccent is called schema-qualified
-- here for the same reason the fix exists: nothing resolves unqualified with search_path empty.
DO $do$
DECLARE fn_schema text; got text;
BEGIN
  SELECT n.nspname INTO fn_schema
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE p.proname = 'f_unaccent' AND pg_get_function_identity_arguments(p.oid) = 'text';
  PERFORM set_config('search_path', '', true);
  EXECUTE format('SELECT %I.f_unaccent(%L)', fn_schema, 'Café') INTO got;
  IF got <> 'Cafe' THEN
    RAISE EXCEPTION 'f_unaccent returned % under an empty search_path, expected Cafe', got;
  END IF;
END $do$;

COMMIT;
