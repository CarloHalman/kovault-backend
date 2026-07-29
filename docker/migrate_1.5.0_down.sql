-- Kovault 1.5.0 DOWN migration — reverts migrate_1.5.0.sql's SCHEMA SHAPE, not its data. Run only
-- if rolling back to a pre-1.5.0 server. Idempotent: safe to run more than once.
--
--   psql "$KOVAULT_DSN" -f migrate_1.5.0_down.sql
--
-- UNRECOVERABLE DATA (read before running):
--   - E1: the ~35 tasks.scope rows the up-migration nulled (the old silent 'minutes' default) do
--     NOT come back as 'minutes' — nothing distinguishes them from a task whose scope was always
--     unset. They stay NULL.
--   - E1: any scope value written after the 1.5.0 upgrade that is not one of the old task_scope
--     labels ('minutes','hours','days','weeks') cannot cast back into that enum. This migration
--     blanks those values to NULL before converting the column back — see the UPDATE below. That
--     freeform text (e.g. "1 sprint") is lost.
--   - E2: groups.status has no pre-1.5.0 equivalent, so every value ever written to it is simply
--     dropped along with the column.
--   - E3: the hot/warm/cold distinction is GONE. 1.5.0 collapsed all three into lifecycle='live',
--     so every live page comes back as 'hot' — the single value the old column defaulted to.
--     Nothing is lost permanently: hot/warm/cold was always derived from page age, so the
--     pre-1.5.0 server's `/janitor -freshness` recomputes the buckets on its first run.
--   - E3: lifecycle on headers/tasks/decisions/sources is dropped with the column — the old shape
--     has no lifecycle for them at all, so an archived or static task/source/decision/chunk comes
--     back indistinguishable from a live one.
--   - E3: pages/groups trashed_at is dropped. A trashed page maps back to freshness='trashed'. A
--     trashed GROUP comes back as archived_at (the old shape has no group trash state, only
--     archive), and archived_at is stamped now() — the original archive time was never carried
--     forward by the up-migration.
--   - E3: the freshness_days / freshness_auto settings rows the up-migration deleted are NOT
--     restored here; 02-init.sql's defaults (and db.py's fallbacks) reseed them on the old server.

BEGIN;

-- ==================== E1: scope becomes freeform (down) ====================
-- Recreate the enum first (guarded: CREATE TYPE has no IF NOT EXISTS).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_scope') THEN
    CREATE TYPE task_scope AS ENUM ('minutes', 'hours', 'days', 'weeks');
  END IF;
END $$;

-- Unrecoverable step (see header): a freeform value that isn't one of the four old labels can't
-- cast to the enum and would abort the ALTER below, so it is blanked here instead.
UPDATE tasks SET scope = NULL
  WHERE scope IS NOT NULL AND scope NOT IN ('minutes', 'hours', 'days', 'weeks');

-- Guarded: only convert while the column is still the 1.5.0 varchar shape.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'tasks' AND column_name = 'scope' AND udt_name <> 'task_scope') THEN
    ALTER TABLE tasks ALTER COLUMN scope TYPE task_scope USING scope::task_scope;
  END IF;
END $$;

-- ==================== E2: groups.status (down) ====================
ALTER TABLE groups DROP COLUMN IF EXISTS status;
DROP TYPE IF EXISTS group_status;

-- ==================== E3: one lifecycle model (down) ====================
-- Restore the old shape: pages.freshness + groups.archived_at, and the two trigger functions the
-- pre-1.5.0 server expects. Read the UNRECOVERABLE DATA notes in the header first.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'page_freshness') THEN
    CREATE TYPE page_freshness AS ENUM ('hot', 'warm', 'cold', 'static', 'archived', 'trashed');
  END IF;
END $$;

ALTER TABLE pages  ADD COLUMN IF NOT EXISTS freshness page_freshness NOT NULL DEFAULT 'hot';
ALTER TABLE groups ADD COLUMN IF NOT EXISTS archived_at timestamptz;

-- Map the 1.5.0 state back while both shapes exist. Guarded on pages.lifecycle so a re-run (when
-- lifecycle is already dropped) skips it, and run through EXECUTE so the statements are never
-- parsed in that case. The generic set_updated_at() is still installed at this point, so the flag
-- keeps these state writes from bumping updated_at — same rule as the up-migration.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'pages' AND column_name = 'lifecycle') THEN
    PERFORM set_config('kovault.state_only', 'on', true);
    EXECUTE $q$UPDATE pages SET freshness = CASE
                 WHEN trashed_at IS NOT NULL   THEN 'trashed'
                 WHEN lifecycle = 'archived'   THEN 'archived'
                 WHEN lifecycle = 'static'     THEN 'static'
                 ELSE 'hot' END::page_freshness$q$;
    EXECUTE $q$UPDATE groups SET archived_at = now()
                 WHERE archived_at IS NULL AND (lifecycle = 'archived' OR trashed_at IS NOT NULL)$q$;
    PERFORM set_config('kovault.state_only', 'off', true);
  END IF;
END $$;

ALTER TABLE pages     DROP COLUMN IF EXISTS lifecycle;
ALTER TABLE headers   DROP COLUMN IF EXISTS lifecycle;
ALTER TABLE tasks     DROP COLUMN IF EXISTS lifecycle;
ALTER TABLE decisions DROP COLUMN IF EXISTS lifecycle;
ALTER TABLE sources   DROP COLUMN IF EXISTS lifecycle;
ALTER TABLE groups    DROP COLUMN IF EXISTS lifecycle;
ALTER TABLE pages     DROP COLUMN IF EXISTS trashed_at;   -- pages/groups had none pre-1.5.0;
ALTER TABLE groups    DROP COLUMN IF EXISTS trashed_at;   --   headers/tasks/decisions/sources keep theirs
DROP TYPE IF EXISTS lifecycle_kind;

-- Restore the pre-1.5.0 trigger pair: unconditional bump everywhere, plus the pages variant that
-- skipped a freshness-only change so the age freshness derives from did not erase itself.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION set_updated_at_pages() RETURNS trigger AS $$
BEGIN
  IF NEW.freshness     IS DISTINCT FROM OLD.freshness
     AND NEW.title        IS NOT DISTINCT FROM OLD.title
     AND NEW.summary      IS NOT DISTINCT FROM OLD.summary
     AND NEW.type         IS NOT DISTINCT FROM OLD.type
     AND NEW.contributors IS NOT DISTINCT FROM OLD.contributors THEN
    NEW.updated_at = OLD.updated_at;
  ELSE
    NEW.updated_at = now();
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pages_updated ON pages;
CREATE TRIGGER trg_pages_updated BEFORE UPDATE ON pages FOR EACH ROW EXECUTE FUNCTION set_updated_at_pages();

COMMIT;
