-- Kovault database schema — PostgreSQL DDL (canonical)
--
-- Target: PostgreSQL 16 + pgvector + pg_search. Apache AGE was dropped (BUILD.md B5) in
--   favour of a recursive CTE over `links` (≤3 hops == the hop-decay cap). To re-add AGE:
--   uncomment the age lines here and in 02-init.sql, and switch the DB image (see README).
-- Ids: gen_random_uuid() (PG16 built-in). Time-ordering comes from created_at, not the uuid.
-- Container load order: 01-schema.sql (this file), then 02-init.sql (settings seed) — see docker-compose.yml.
--
-- Lifecycle model (E3), identical on every content table (pages, headers, tasks, decisions,
-- sources, groups): `trashed_at` (null = live) is the ONE trash marker — nothing is ever
-- hard-deleted — and `lifecycle` (live/archived/static) is the orthogonal state. Every trash,
-- revive and lifecycle change logs an edits row and, being a state-only write, leaves
-- updated_at alone (see the set_updated_at() trigger at the bottom of this file).

-- ---------------- Extensions ----------------
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector (halfvec)
CREATE EXTENSION IF NOT EXISTS pg_search;   -- BM25 full-text (needs shared_preload_libraries, see docker-compose.yml)
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram index for the graph anchor ILIKE (perf; stock contrib, bundled in paradedb)
CREATE EXTENSION IF NOT EXISTS unaccent;    -- accent-folding for the normalized keyword arm (F2)
-- CREATE EXTENSION IF NOT EXISTS age;      -- graph engine — only if AGE is re-added (BUILD.md B5)

-- IMMUTABLE unaccent wrapper: bare unaccent() is only STABLE, so it cannot be used in a generated
-- column or expression index. Naming the dictionary explicitly makes it safe to mark IMMUTABLE.
--
-- search_path is PINNED to the schema unaccent actually landed in, and that pin is load-bearing:
-- pg_restore runs with an empty search_path, so an unpinned body cannot resolve either unaccent()
-- or the 'unaccent' regdictionary, and every table below carrying a *_norm generated column fails
-- to create — the restore of a Kovault backup collapses. Resolved dynamically rather than written
-- as public.unaccent(...) so it still holds when the extension lives in a non-public schema.
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

-- ---------------- Enums ----------------
CREATE TYPE entity_kind      AS ENUM ('source', 'task', 'page', 'decision');
CREATE TYPE source_type      AS ENUM ('website', 'file', 'server', 'database');
CREATE TYPE change_operation AS ENUM ('insert', 'update', 'trash');   -- nothing hard-deletes
CREATE TYPE actor_kind       AS ENUM ('self', 'ai', 'script');
CREATE TYPE task_status      AS ENUM ('todo', 'doing', 'done');
CREATE TYPE task_priority    AS ENUM ('low', 'medium', 'high', 'urgent');
CREATE TYPE lifecycle_kind   AS ENUM ('live', 'archived', 'static');  -- E3: same on every content table
CREATE TYPE group_types      AS ENUM ('project', 'topic', 'area');
CREATE TYPE group_status     AS ENUM ('active', 'completed', 'idle');  -- nullable on groups; aimed at type: project (E2)
CREATE TYPE link_kind        AS ENUM ('page', 'header', 'task', 'decision', 'source');

-- ---------------- Tables ----------------

CREATE TABLE entities (
  id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  kind       entity_kind NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE groups (
  id           uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at   timestamptz   NOT NULL DEFAULT now(),
  updated_at   timestamptz   NOT NULL DEFAULT now(),
  trashed_at   timestamptz,                             -- null = live (E3)
  lifecycle    lifecycle_kind NOT NULL DEFAULT 'live',  -- archived drops it from default lists (E3)
  name         varchar(64)   NOT NULL,
  type         group_types   NOT NULL,                  -- loose, flexible categories
  description  varchar(512),
  participants varchar(64)[],
  status       group_status                             -- nullable, no default; workflow state (E2), aimed at type: project
);

CREATE TABLE group_links (
  group_id   uuid        NOT NULL REFERENCES groups(id)   ON DELETE CASCADE,
  entity_id  uuid        NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (group_id, entity_id)
);

CREATE TABLE sources (
  id                uuid        PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  trashed_at        timestamptz,                         -- null = live
  lifecycle         lifecycle_kind NOT NULL DEFAULT 'live',
  type              source_type NOT NULL,
  title             varchar(64),
  reference         varchar(256) NOT NULL,               -- url, path
  sha256            char(64),                            -- ingest dedupe key
  summary           varchar(512),                        -- re-ingest dates appended here
  title_norm        text GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED,  -- F2 trigram
  summary_embedding halfvec(4000),                       -- embed: composed from row fields (embedding.md)
  embedded_at       timestamptz                          -- < updated_at → stale, /janitor -embed re-embeds
);

CREATE TABLE decisions (
  id          uuid         PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  created_at  timestamptz  NOT NULL DEFAULT now(),
  updated_at  timestamptz  NOT NULL DEFAULT now(),
  trashed_at  timestamptz,
  lifecycle   lifecycle_kind NOT NULL DEFAULT 'live',
  title       varchar(64) NOT NULL,
  description varchar(1024),
  decided_by  varchar(64),
  decided_at  timestamptz,
  title_norm  text GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED,  -- F2 trigram
  embedding   halfvec(4000),
  embedded_at timestamptz
);

CREATE TABLE edits (
  id          uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at  timestamptz      NOT NULL DEFAULT now(),
  table_name  varchar(64)      NOT NULL,
  row_id      uuid             NOT NULL,
  operation   change_operation NOT NULL,
  changes     jsonb,
  edited_by   varchar(64)      NOT NULL,                 -- username from the local config
  actor       actor_kind       NOT NULL DEFAULT 'self'
);
CREATE INDEX ON edits (table_name, row_id);              -- per-row history lookups

CREATE TABLE tasks (
  id          uuid          PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  created_at  timestamptz   NOT NULL DEFAULT now(),
  updated_at  timestamptz   NOT NULL DEFAULT now(),
  trashed_at  timestamptz,
  lifecycle   lifecycle_kind NOT NULL DEFAULT 'live',
  title       varchar(64)  NOT NULL,
  description varchar(1024),
  status      task_status   NOT NULL DEFAULT 'todo',
  priority    task_priority,                             -- nullable (F4): unset != a deliberate 'medium'
  scope       varchar(16),                               -- freeform (E1), nullable: no closed set, unset != a deliberate value
  deadline    timestamptz,
  completed_at timestamptz,                              -- stamped by trigger when status -> done (F4)
  responsible varchar(64)[],                             -- free names; may name people without Kovault access
  title_norm  text GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED,  -- F2 trigram
  embedding   halfvec(4000),
  embedded_at timestamptz
);

CREATE TABLE task_dependencies (
  created_at timestamptz NOT NULL DEFAULT now(),
  blocker    uuid        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, -- blocks the dependent
  dependent  uuid        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, -- locked behind blocker
  PRIMARY KEY (blocker, dependent)
);

CREATE TABLE pages (
  id           uuid           PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  created_at   timestamptz    NOT NULL DEFAULT now(),
  updated_at   timestamptz    NOT NULL DEFAULT now(),
  trashed_at   timestamptz,                              -- null = live (E3; was freshness='trashed')
  lifecycle    lifecycle_kind NOT NULL DEFAULT 'live',   -- archived drops out of lookup (E3)
  title        varchar(64)   NOT NULL,                  -- feeds every chunk's path + embedding
  summary      varchar(512),
  type         varchar(32),                              -- OKF-style: free descriptive value
  contributors varchar(64)[]                             -- append-only
);

CREATE TABLE headers (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  trashed_at  timestamptz,
  lifecycle   lifecycle_kind NOT NULL DEFAULT 'live',
  page_id     uuid        NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  title       varchar(64),                              -- short label; models keep titles <=64 (formatting rule in CLAUDE.md), full heading lives in body
  index       int         NOT NULL,                      -- position on page
  level       int         NOT NULL,                      -- heading level
  path        varchar(512),                              -- page.title > H1 > H2 > H3
  blurb       varchar(256),
  body        text,                                      -- markdown links in here become graph edges
  title_norm  text GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(title,''),'[-\s]+','','g')))) STORED,  -- F2 trigram
  blurb_norm  text GENERATED ALWAYS AS (lower(f_unaccent(regexp_replace(coalesce(blurb,''),'[-\s]+','','g')))) STORED,
  embedding   halfvec(4000),
  embedded_at timestamptz
);
-- Position is unique only among LIVE headers: delete() trashes via trashed_at and never
-- clears index, so trashed headers keep their old index and must be excluded from the
-- constraint (otherwise reordering/reinsert around a trashed chunk collides).
CREATE UNIQUE INDEX ON headers (page_id, index) WHERE trashed_at IS NULL;

CREATE TABLE header_sources (
  header_id uuid NOT NULL REFERENCES headers(id) ON DELETE CASCADE,
  source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  PRIMARY KEY (header_id, source_id)
);

-- Graph edges. Polymorphic (any row kind → any row kind), so no FKs — the linking
-- script validates targets and skips dangling links. Populated automatically by
-- parsing markdown links out of body/description text.
CREATE TABLE links (
  from_kind  link_kind   NOT NULL,
  from_id    uuid        NOT NULL,
  to_kind    link_kind   NOT NULL,
  to_id      uuid        NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (from_kind, from_id, to_kind, to_id)
);
CREATE INDEX ON links (to_kind, to_id);                  -- reverse traversal

-- Every /janitor run logs here (never into decisions — knowledge stays clean).
CREATE TABLE janitor_reports (
  id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  flags      varchar(16)[],                              -- empty/null = diagnose-only run
  report     text        NOT NULL,                       -- report + advice
  counts     jsonb                                       -- rows touched per check/fix
);

-- Server-wide settings, admin-tunable. Seeded by 02-init.sql.
CREATE TABLE settings (
  key        varchar(64) PRIMARY KEY,
  value      jsonb       NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Debug query log (opt-in; local `debug` config flag, default off). Unlike
-- `edits` (mutations only), this captures EVERY Kovault tool call so query patterns, timings, and the
-- conversation context that triggered them can be reviewed. Written by the plugin's PostToolUse
-- hook via the /debug-log route (the client holds the transcript the server never sees).
CREATE TABLE debug_log (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  session_id     text,                        -- groups a conversation's queries (from the hook)
  edited_by      varchar(64),                 -- username
  tool           varchar(64) NOT NULL,        -- lookup / fetch / snippet / insert / ...
  tool_input     jsonb,                        -- what was asked (e.g. the lookup terms / ids)
  result_summary text,                         -- short shape of the result (counts / bytes)
  result         text,                          -- raw tool result (server return), capped; token
                                                --   cost is recomputable from this + the raw fields
  result_tokens  integer,                      -- convenience estimate of result token cost (chars/4)
  duration_ms    integer,                      -- client-measured tool-call round-trip latency
                                                --   (plugin hook), NOT server compute time
  last_user_msg  text,                         -- the user message that led here
  assistant_text text                          -- Claude's text this turn up to the tool call
);

-- ---------------- Vector indexes (pgvector hnsw, halfvec) ----------------
CREATE INDEX ON sources   USING hnsw (summary_embedding halfvec_cosine_ops);
CREATE INDEX ON decisions USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX ON tasks     USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX ON headers   USING hnsw (embedding halfvec_cosine_ops);

-- ---------------- Graph anchor index (pg_trgm) ----------------
-- The graph signal anchors on headers.title ILIKE '%term%' (search.md). Without this that is
-- a seq-scan that grows with the corpus; the trigram GIN index makes it an index scan and keeps
-- graph scoring flat as data grows (measured ~4x at 5.5k chunks).
CREATE INDEX ON headers USING gin (title gin_trgm_ops);

-- Normalized-title trigram indexes for the fuzzy keyword arm (F2). The `%`/similarity() probes in
-- _trigram_hits use these; without them each is a seq-scan.
-- Named explicitly to match migrate_1.3.0.sql. `CREATE INDEX ON` auto-names to *_title_norm_idx,
-- so a FRESH install and a MIGRATED one ended up with different names for the same index, and a
-- later migration dropping one by name would silently no-op on half the installs.
CREATE INDEX headers_title_norm_trgm   ON headers   USING gin (title_norm gin_trgm_ops);
CREATE INDEX headers_blurb_norm_trgm   ON headers   USING gin (blurb_norm gin_trgm_ops);
CREATE INDEX tasks_title_norm_trgm     ON tasks     USING gin (title_norm gin_trgm_ops);
CREATE INDEX decisions_title_norm_trgm ON decisions USING gin (title_norm gin_trgm_ops);
CREATE INDEX sources_title_norm_trgm   ON sources   USING gin (title_norm gin_trgm_ops);

-- ---------------- Keyword indexes (pg_search bm25) ----------------
CREATE INDEX ON headers   USING bm25 (id, title, blurb, body)         WITH (key_field='id');
CREATE INDEX ON tasks     USING bm25 (id, title, description)         WITH (key_field='id');
CREATE INDEX ON decisions USING bm25 (id, title, description)         WITH (key_field='id');
CREATE INDEX ON sources   USING bm25 (id, title, summary, reference)  WITH (key_field='id');

-- ---------------- Graph ----------------
-- links is the durable edge store. Traversal is a recursive CTE over links (≤3 hops —
-- matches the hop-decay cap); see ../mcp-server/kovault_mcp/search.py (GRAPH_BFS_SQL).
-- No AGE: identical scores at Kovault scale, no custom image, no projection sync (BUILD.md B5).

-- ---------------- updated_at trigger ----------------
-- The updated_at rule (E3): a CONTENT write bumps updated_at; a STATE-ONLY write (trash, revive,
-- lifecycle change, every janitor pass) must not — otherwise a state flip makes the row look
-- "changed this week" and marks it embed-stale via `embedded_at < updated_at` (embed_worker.py).
-- The writer declares its own intent with `SET LOCAL kovault.state_only = 'on'` around the
-- statement; the trigger does not try to guess. A per-table "did a content column change?" test
-- would be the hand-maintained field list this release deletes everywhere else, and doing it
-- generically (to_jsonb(OLD) vs to_jsonb(NEW)) would serialize a halfvec(4000) on every UPDATE.
-- SET LOCAL is transaction-scoped and every server write runs in a transaction (db.py opens the
-- pool with autocommit=False), so the flag can never leak to the next borrower of the connection.
-- A hand-written UPDATE from psql sets no flag and therefore bumps updated_at — the right default.
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

CREATE TRIGGER trg_pages_updated     BEFORE UPDATE ON pages     FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_headers_updated   BEFORE UPDATE ON headers   FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_tasks_updated     BEFORE UPDATE ON tasks     FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_decisions_updated BEFORE UPDATE ON decisions FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_sources_updated   BEFORE UPDATE ON sources   FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_groups_updated    BEFORE UPDATE ON groups    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_settings_updated  BEFORE UPDATE ON settings  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Task completion stamp (F4): set completed_at when a task first reaches 'done'; clear it if the
-- task is later reopened. Automates the "done on" timestamp without a planned_start/doing sweep.
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

CREATE TRIGGER trg_tasks_completed BEFORE INSERT OR UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION set_task_completed_at();
