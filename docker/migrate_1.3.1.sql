-- Kovault 1.3.1 migration — apply to an EXISTING database BEFORE deploying the 1.3.1 server image.
-- 01-schema.sql only runs on a fresh container, so a live DB needs these ALTERs. Idempotent: safe
-- to run more than once. Run migrate_1.3.0.sql first if the DB has not had it.
--
--   psql "$KOVAULT_DSN" -f migrate_1.3.1.sql
--   (or: docker compose exec kovault-db psql -U kovault -d kovault -f /path/migrate_1.3.1.sql)

BEGIN;

-- ---- A3: group archive ----------------------------------------------------------------
-- groups gain an archived_at marker; the group tool's `archive`/`unarchive` action and
-- `write trashed: true` write it, and default `group list` hides archived rows.
ALTER TABLE groups ADD COLUMN IF NOT EXISTS archived_at timestamptz;

-- ---- A2: people normalization backfill ------------------------------------------------
-- Contributors was append-only, so case/variant duplicates baked in (carlo/Carlo,
-- quincy/Quincy/QuincyK). Case-fold (lowercase, matching the write-boundary norm) + dedupe
-- every person-bearing column. Same transform as `janitor -normalize-people`; run once here so
-- the deploy lands clean. Only rewrites rows that actually change.
UPDATE pages t SET contributors = sub.arr
  FROM (SELECT id, ARRAY(SELECT DISTINCT lower(x) FROM unnest(contributors) x
                         WHERE x IS NOT NULL AND x <> '') arr
        FROM pages WHERE contributors IS NOT NULL) sub
  WHERE t.id = sub.id AND t.contributors IS DISTINCT FROM sub.arr;

UPDATE tasks t SET responsible = sub.arr
  FROM (SELECT id, ARRAY(SELECT DISTINCT lower(x) FROM unnest(responsible) x
                         WHERE x IS NOT NULL AND x <> '') arr
        FROM tasks WHERE responsible IS NOT NULL) sub
  WHERE t.id = sub.id AND t.responsible IS DISTINCT FROM sub.arr;

UPDATE groups t SET participants = sub.arr
  FROM (SELECT id, ARRAY(SELECT DISTINCT lower(x) FROM unnest(participants) x
                         WHERE x IS NOT NULL AND x <> '') arr
        FROM groups WHERE participants IS NOT NULL) sub
  WHERE t.id = sub.id AND t.participants IS DISTINCT FROM sub.arr;

UPDATE decisions SET decided_by = lower(decided_by)
  WHERE decided_by IS DISTINCT FROM lower(decided_by);

COMMIT;
