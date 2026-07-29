-- Kovault 1.5.0 migration — apply to an EXISTING database BEFORE deploying the 1.5.0 server image.
-- 01-schema.sql only runs on a fresh container, so a live DB needs these ALTERs. Idempotent: safe
-- to run more than once. Run migrate_1.3.1.sql first if the DB has not had it (and 1.3.0 before that).
--
--   psql "$KOVAULT_DSN" -f migrate_1.5.0.sql
--   (or: docker compose exec kovault-db psql -U kovault -d kovault -f /path/migrate_1.5.0.sql)
--
-- Sections are banner-tagged (E1, E2, ...) because phase 1's later tasks (E3+) append their own
-- sections to this same file, above the final COMMIT.

BEGIN;

-- ==================== E1: scope becomes freeform ====================
-- tasks.scope drops the closed task_scope enum for a free varchar(16) — scope is open text
-- ("1 sprint", "2 weeks", ...), not a fixed set. Guarded: ALTER COLUMN TYPE has no IF NOT EXISTS,
-- so only run it while the column is still the old enum type.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'tasks' AND column_name = 'scope' AND udt_name = 'task_scope') THEN
    ALTER TABLE tasks ALTER COLUMN scope TYPE varchar(16) USING scope::text;
  END IF;
END $$;

DROP TYPE IF EXISTS task_scope;

-- E1's data backfill is NOT here: it is a state backfill like E3's, so it must not bump
-- updated_at, and the flag that suppresses that only works once E3 has installed the new trigger
-- function. It runs at the end of the E3 section instead.

-- ==================== E2: groups.status ====================
-- Nullable workflow status on groups (active/completed/idle), no default. Aimed at type: project;
-- rendering is out of scope here (phase 3). Registered as a real enum (group_status) so the
-- existing _ENUM_COLS / _check_enums machinery validates and normalizes it, same as task_status.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'group_status') THEN
    CREATE TYPE group_status AS ENUM ('active', 'completed', 'idle');
  END IF;
END $$;

ALTER TABLE groups ADD COLUMN IF NOT EXISTS status group_status;

-- ==================== E3: one lifecycle model across every table ====================
-- Two state columns, identical on all six content tables (pages, headers, tasks, decisions,
-- sources, groups), replacing pages.freshness and groups.archived_at:
--   trashed_at  — the ONE trash marker (null = live), so every entity trashes and revives alike
--   lifecycle   — live / archived / static, orthogonal to trash
-- Uniformity is the point: the reflective renderer and column reflection later in 1.5.0 assume
-- every content table has the same shape.

-- The enum is cross-table, so it takes the cross-table naming convention (entity_kind,
-- actor_kind, link_kind) rather than a table prefix like the old page_freshness.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lifecycle_kind') THEN
    CREATE TYPE lifecycle_kind AS ENUM ('live', 'archived', 'static');
  END IF;
END $$;

-- headers/tasks/decisions/sources already carry trashed_at; pages and groups did not.
ALTER TABLE pages     ADD COLUMN IF NOT EXISTS trashed_at timestamptz;
ALTER TABLE groups    ADD COLUMN IF NOT EXISTS trashed_at timestamptz;

ALTER TABLE pages     ADD COLUMN IF NOT EXISTS lifecycle lifecycle_kind NOT NULL DEFAULT 'live';
ALTER TABLE headers   ADD COLUMN IF NOT EXISTS lifecycle lifecycle_kind NOT NULL DEFAULT 'live';
ALTER TABLE tasks     ADD COLUMN IF NOT EXISTS lifecycle lifecycle_kind NOT NULL DEFAULT 'live';
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS lifecycle lifecycle_kind NOT NULL DEFAULT 'live';
ALTER TABLE sources   ADD COLUMN IF NOT EXISTS lifecycle lifecycle_kind NOT NULL DEFAULT 'live';
ALTER TABLE groups    ADD COLUMN IF NOT EXISTS lifecycle lifecycle_kind NOT NULL DEFAULT 'live';

-- The updated_at rule. One generic trigger function replaces set_updated_at() AND
-- set_updated_at_pages(): a CONTENT write bumps updated_at, a STATE-ONLY write (trash, revive,
-- lifecycle change, every janitor pass) does not — otherwise a state flip makes the row look
-- "changed this week" and marks it embed-stale via `embedded_at < updated_at`. The writer
-- declares intent with `SET LOCAL kovault.state_only = 'on'`; the trigger never has to guess,
-- so there is no hand-maintained per-table content-column list and no to_jsonb() of a
-- halfvec(4000) on every UPDATE. SET LOCAL is transaction-scoped and the server pool runs with
-- autocommit=False, so the flag cannot leak to the next borrower of a pooled connection.
-- A hand-written UPDATE from psql sets no flag and therefore bumps updated_at — correct default.
-- Created BEFORE the backfill below, because the OLD pages trigger would bump updated_at on
-- every backfilled row (its guard only skips a freshness-ONLY change).
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  IF coalesce(current_setting('kovault.state_only', true), '') = 'on' THEN
    NEW.updated_at = OLD.updated_at;
  ELSE
    NEW.updated_at = now();
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Repoint pages at the generic function, then drop the pages-only variant (it references
-- NEW.freshness and would break every pages UPDATE the moment that column is dropped below).
DROP TRIGGER IF EXISTS trg_pages_updated ON pages;
CREATE TRIGGER trg_pages_updated BEFORE UPDATE ON pages FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP FUNCTION IF EXISTS set_updated_at_pages();

-- Data migration. Guarded on the old column still existing (idempotent: a re-run skips it), and
-- run through EXECUTE so the statements are never parsed once `freshness` is gone. set_config(
-- ..., true) is SET LOCAL in expression form: these are state backfills, not content edits, so
-- they must not touch updated_at — the whole reason the age-derived buckets existed.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'pages' AND column_name = 'freshness') THEN
    PERFORM set_config('kovault.state_only', 'on', true);
    EXECUTE $q$UPDATE pages SET trashed_at = now() WHERE freshness = 'trashed' AND trashed_at IS NULL$q$;
    EXECUTE $q$UPDATE pages SET lifecycle = freshness::text::lifecycle_kind WHERE freshness IN ('archived','static')$q$;
    EXECUTE $q$UPDATE pages SET lifecycle = 'live' WHERE freshness IN ('hot','warm','cold')$q$;
    PERFORM set_config('kovault.state_only', 'off', true);
  END IF;
END $$;

-- groups.archived_at -> lifecycle='archived'. The timestamp itself is carried NOWHERE: lifecycle
-- is a state, not a date, and `edits` already records when the archive happened.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'groups' AND column_name = 'archived_at') THEN
    PERFORM set_config('kovault.state_only', 'on', true);
    EXECUTE $q$UPDATE groups SET lifecycle = 'archived' WHERE archived_at IS NOT NULL$q$;
    PERFORM set_config('kovault.state_only', 'off', true);
  END IF;
END $$;

ALTER TABLE pages  DROP COLUMN IF EXISTS freshness;
ALTER TABLE groups DROP COLUMN IF EXISTS archived_at;
DROP TYPE IF EXISTS page_freshness;

-- The auto-freshness machinery is gone with the column (no hot/warm/cold to recompute), so its
-- settings rows are dead weight — including the runtime stamp the cooldown claimed.
DELETE FROM settings WHERE key IN ('freshness_days', 'freshness_auto', 'freshness_last_auto');

-- ---- E1's backfill, deferred to here (see the note in the E1 section) --------------------
-- Null out only the 'minutes' rows that were NEVER deliberately written. The column was
-- NOT NULL DEFAULT 'minutes' until v1.3.0 (38b5767) made it nullable, so every task inserted before
-- that carries the old default whether or not anyone chose it. The `edits` audit log distinguishes
-- a genuine write (scope logged in that row's changes) from the silent default (nothing logged).
-- Runs here, after E1's type change AND after E3's trigger exists, bracketed by the state-only
-- flag: clearing a stale default is a maintenance backfill, not a content edit, so it must not
-- bump updated_at and send ~35 rows back through the embedder. Idempotent: once a row is nulled
-- it no longer matches `scope = 'minutes'` on a re-run.
DO $$
BEGIN
  PERFORM set_config('kovault.state_only', 'on', true);
  UPDATE tasks t SET scope = NULL
    WHERE t.scope = 'minutes'
      AND NOT EXISTS (SELECT 1 FROM edits e
                      WHERE e.table_name = 'tasks' AND e.row_id = t.id
                        AND e.changes->>'scope' = 'minutes');
  PERFORM set_config('kovault.state_only', 'off', true);
END $$;

COMMIT;
