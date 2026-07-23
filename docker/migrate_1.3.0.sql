-- Kovault 1.3.0 migration — apply to an EXISTING database BEFORE deploying the 1.3.0 server image.
-- 01-schema.sql only runs on a fresh container, so a live DB needs these ALTERs. Idempotent: safe
-- to run more than once. Reason it must run first: the 1.3.0 insert path writes NULL scope/priority,
-- which the pre-1.3.0 NOT NULL constraint would reject.
--
--   psql "$KOVAULT_DSN" -f migrate_1.3.0.sql
--   (or: docker compose exec kovault-db psql -U kovault -d kovault -f /path/migrate_1.3.0.sql)

BEGIN;

-- ---- F4: task model -------------------------------------------------------------------
-- scope/priority become nullable (an unset field must be distinguishable from a deliberate choice).
ALTER TABLE tasks ALTER COLUMN priority DROP NOT NULL;
ALTER TABLE tasks ALTER COLUMN priority DROP DEFAULT;
ALTER TABLE tasks ALTER COLUMN scope    DROP NOT NULL;
ALTER TABLE tasks ALTER COLUMN scope    DROP DEFAULT;
-- (existing rows are intentionally NOT backfilled to NULL: a real 'medium' can't be told from a default.)

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at timestamptz;

CREATE OR REPLACE FUNCTION set_task_completed_at() RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'done' AND NEW.completed_at IS NULL
     AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'done') THEN
    NEW.completed_at = now();
  ELSIF TG_OP = 'UPDATE' AND NEW.status <> 'done' AND OLD.status = 'done' THEN
    NEW.completed_at = NULL;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_completed ON tasks;
CREATE TRIGGER trg_tasks_completed BEFORE INSERT OR UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION set_task_completed_at();

-- ---- F2: keyword recall (normalized trigram arm) --------------------------------------
-- unaccent is stock contrib and ships in the paradedb image; this errors loudly if absent
-- (check first with: SELECT 1 FROM pg_available_extensions WHERE name='unaccent').
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT unaccent('unaccent', $1) $$;

-- Adding a STORED generated column rewrites the table (seconds at a few-k rows) and fills it in.
ALTER TABLE headers   ADD COLUMN IF NOT EXISTS title_norm text
  GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED;
ALTER TABLE headers   ADD COLUMN IF NOT EXISTS blurb_norm text
  GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(blurb,''),'[-\s]+','','g')))) STORED;
ALTER TABLE tasks     ADD COLUMN IF NOT EXISTS title_norm text
  GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS title_norm text
  GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED;
ALTER TABLE sources   ADD COLUMN IF NOT EXISTS title_norm text
  GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED;

CREATE INDEX IF NOT EXISTS headers_title_norm_trgm   ON headers   USING gin (title_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS headers_blurb_norm_trgm   ON headers   USING gin (blurb_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS tasks_title_norm_trgm     ON tasks     USING gin (title_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS decisions_title_norm_trgm ON decisions USING gin (title_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS sources_title_norm_trgm   ON sources   USING gin (title_norm gin_trgm_ops);

COMMIT;
