# Changelog

## 1.5.1

Documentation fixes found by installing 1.5.0 from a clean clone and following the README
literally. No code or schema change; the server is identical to 1.5.0.

1. **The install steps did not run as written.** Steps 2, 4, 5 and 6 each began with `cd docker`,
   so every one after the first failed with `No such file or directory`. The directory change is
   now stated once, and step 3 says how to get back from `embedding/`.

2. **The secret files were copied but never edited.** Step 2 copied
   `secrets/kovault_db_password.txt.example` and `secrets/kovault_auth_token.txt.example` into
   place and no later step said to change them, so anyone following the README exactly deployed
   with the published placeholders `change-me-before-production` and
   `change-me-to-a-long-random-string` as their database password and auth token. Step 2 now
   generates both with `openssl rand -base64 32` instead of copying, and the compose file's
   comments say the same. **If you installed 1.5.0 by following the README, check
   `docker/secrets/` for those two strings and replace them.**

3. **Docker Desktop on Windows** must be able to see the directory you clone into. Where it
   cannot, the secret mounts fail with `bind source path does not exist` naming a file that is
   present on disk. Noted in the prerequisites.

## 1.5.0

This file covers the `kovault-backend` half of the release. The plugin's half is `kovault`'s
own `CHANGELOG.md`.

### Breaking changes

1. **`freshness` gone.** `pages.freshness` (hot/warm/cold/static/archived/trashed) is dropped.
   Every content table — pages, headers, tasks, decisions, sources, groups — now carries
   `trashed_at` (null = live) and `lifecycle` (live/archived/static) instead, so trash/revive/
   lifecycle work identically everywhere, not just on pages. A page write that still sends
   `freshness:` does not error: the key is unrecognized, so the value is silently dropped and the
   write's result line carries a `warning: unknown key 'freshness' ... — value dropped` note
   while the rest of the block still commits. A caller that doesn't surface warnings will believe
   the change took effect when it did not. Send `lifecycle:`/`trashed:` instead — mapping below.
2. **`archived_at` gone.** `groups.archived_at` is dropped; use `lifecycle: archived` on the group
   instead. Archiving a group now cascades: any task left with no other live group is archived
   too (state-only — doesn't touch the task's `updated_at`).
3. **`scope` is no longer an enum.** `tasks.scope` was the closed `task_scope` enum
   (minutes/hours/days/weeks); it's now freeform `varchar(16)` text ("1 sprint", "2 weeks",
   anything up to 16 characters). Existing values keep their meaning. Rows still carrying the
   pre-1.3.0 silent default are nulled during migration — `scope` was `NOT NULL DEFAULT 'minutes'`
   until 1.3.0, so tasks created before then hold a `'minutes'` nobody chose. The `edits` log
   distinguishes a deliberate write from an inherited default, so a scope you actually picked
   survives. How many rows that touches depends on your vault's age.
4. **Five wrapper tools removed:** `insert`, `update`, `delete`, `link`, `group`. Everything they
   did is reachable through `write` (junction rosters and tri-state `trashed:` closed the last
   gaps). Conversion table below.
5. **Authentication is enforced on every route** — `/mcp` and the four custom routes
   (`/export`, `/relocate-sources`, `/page-meta`, `/debug-log`) now require
   `Authorization: Bearer <token>`. An empty token file still runs open (today's behaviour) with a
   loud startup warning naming every exposed surface; set a token to close it. See Migration.
6. **Published ports bind to loopback by default, and the bundled embedder no longer publishes a
   host port at all.** This one fails silently rather than loudly if you miss it — see Migration.
7. **Export frontmatter shape changed:** `trashed:`/`lifecycle:` replace `freshness:`. Anything
   parsing an exported OKF bundle needs updating.

### Migration — for an existing install

**Back up the database first** (`pg_dump` or your usual method). The down migration restores
schema shape, not data — see step 2.

**1. Three deployment steps that hard-fail `docker compose up` if skipped.**

```bash
docker network create kovault-net        # both compose projects now join it, so kovault-mcp
                                         # reaches the embedder without a published port
cd docker
touch secrets/kovault_auth_token.txt     # must EXIST; empty keeps today's no-auth behaviour
cp .env.example .env                     # binds published ports to loopback
```

**2. Run the schema migration** before deploying the 1.5.0 server image:

```bash
psql "$KOVAULT_DSN" -f docker/migrate_1.5.0.sql
```

Idempotent (safe to re-run), ~0.27s rehearsed against a restored copy of a production-sized
vault. It drops `pages.freshness` and `groups.archived_at`, adds `trashed_at`/`lifecycle` to all
six content tables, converts `tasks.scope` to freeform text, and adds `groups.status`.

If you need to roll back to a pre-1.5.0 server, `docker/migrate_1.5.0_down.sql` restores the old
**shape** — it cannot restore the old **data**. Read its header before running it. Unrecoverable
on rollback:

- Freeform `scope` values that aren't one of the four old labels (minutes/hours/days/weeks) are
  blanked to `NULL` before the column converts back to the enum.
- Rows the up migration nulled do not come back as `'minutes'` — nothing distinguishes them
  from a task whose scope was always unset. (The up migration only nulls rows that inherited
  the pre-1.3.0 `NOT NULL DEFAULT 'minutes'`, identified through the `edits` log; a scope
  anyone actually chose is left alone. How many that is depends on your vault.)
- `groups.status` is dropped outright with the column — no pre-1.5.0 equivalent exists for it.
- Every live page comes back `'hot'`; the hot/warm/cold split itself isn't permanently lost,
  since it was always derived from page age and a pre-1.5.0 server's `/janitor -freshness`
  recomputes the buckets on its first run — but nothing that happened while lifecycle was live
  and not "hot" is reconstructed automatically.
- `lifecycle` on headers/tasks/decisions/sources has no pre-1.5.0 home at all: an archived or
  static task, decision, source or chunk comes back indistinguishable from a live one.
- A trashed group comes back as `archived_at = now()`, not the time it was actually archived —
  the original timestamp was never carried forward by the up migration.

**3. The silent one: repoint the embedding endpoint if you use the bundled embedder.** The
bundled embedder no longer publishes port `11434` on the host. If your `embedding` setting still
points at `http://host.docker.internal:11434`, the embedder goes dead the moment you restart on
1.5.0 — **with no error anywhere**: the embed worker treats a dead endpoint as a temporary blip
and backs off forever, so writes keep succeeding and search just quietly stops including anything
written since. Repoint it:

```sql
UPDATE settings SET value = jsonb_set(value, '{endpoint}', '"http://embedding:11434"')
WHERE key = 'embedding';
```

Then `docker compose up -d` both projects and run `/kovault:janitor -embed` to catch up anything
written while it was pointed at nothing. Confirm it took: `/kovault:janitor` reports
`stale_embeddings=0` once the queue has drained.

Would rather keep the host port? Re-add `ports: ["127.0.0.1:11434:11434"]` to
`embedding/docker-compose.yml` and leave the setting alone — but bind it to loopback, not to every
interface as it was before.

**4. Converting an old client off the five removed wrappers:**

| Old tool | Use instead |
|---|---|
| `insert` | `write` with a block that has no `id:` |
| `update` | `write` with a block that has `id:` |
| `delete` | `write` with `id:` and `trashed: true` |
| `group` (create/update/archive) | `write` with a `type: group` block |
| `group list` | `lookup(tables=["groups"], filters=[])` (precise mode) |
| `link` | no call at all — write the reference as `[text](kind:uuid)` or `[[wikilink]]` markdown inside the body/description of a `write` block; the server parses it into a graph edge |

**5. `freshness:` → `lifecycle:`/`trashed:` mapping:**

| Old `freshness:` value | New |
|---|---|
| `hot` / `warm` / `cold` | `lifecycle: live` (the three age-derived buckets collapse into one) |
| `static` | `lifecycle: static` |
| `archived` | `lifecycle: archived` |
| `trashed` | `trashed: true` — tri-state now: omit leaves trash state alone, `true` trashes, an explicitly *empty* `trashed:` line revives. Works identically on every entity, not just pages. |
| group `archived_at` set | `lifecycle: archived` on the group |

Any client still sending `freshness:` breaks functionally, not loudly (see breaking-change #1
above) — switch it to `lifecycle:`/`trashed:`.

**6. Exports.** Anything parsing an OKF export bundle needs updating: exported frontmatter
carries `trashed:` and `lifecycle:` keys where it used to carry `freshness:`.

### New in the write path
- Batched writes are no longer all-or-nothing: each block runs in its own savepoint, so one bad
  block in a multi-block batch fails alone and the response ends with an
  `N committed, M failed` summary — a resend only needs to carry the failed blocks.
- Column-limit and type errors are now reported per field, per block, naming the actual value's
  length and the column's limit, instead of surfacing a raw Postgres error. This includes array
  columns (`responsible`, `participants`, `contributors`), whose element limits weren't checked
  before.
- `=`/`ilike` filtering is now array-aware on `lookup` precise mode and on `rows` — filtering on
  `responsible` used to silently return nothing.
- `members_add:`/`members_remove:` (and the same for a task's `blockers:` and a header's
  `sources:`) touch part of a roster without resending the whole list.
- Canonical-person matching is now one shared function used by identity resolution, the write
  boundary and the janitor pass. Case is preserved as typed; duplicates differing only by case
  collapse together.
- Editing a `todo` task's title or description now flips it to `doing` automatically at the write
  boundary. Triage-only edits (priority/scope/deadline/responsible) do not, and an explicit
  `status:` you send always wins.
- An unlinked task is now offered a plausible page/decision match from the existing trigram
  probe; a planned task with no owner names the default it's about to apply.
- The table-argument quote strip lost in the v1.3 rewrite is restored (now on all three surfaces
  that take a raw table name), with a regression test.

### New in the read path
- The renderer now reflects whatever columns a row actually carries instead of a hand-maintained
  field list per kind — a column added to a table renders with no code change.
- `columns:` parameter on `fetch` and `lookup` precise mode: `["+col","-col"]` adjusts the
  default set, `["col1","col2"]` replaces it outright (the two forms can't be mixed). Naming a
  vector or generated column is refused with a message rather than silently dropped; `id` can't
  be dropped at all.
- ids on the read path are truncated to 8 characters for display; a short unique prefix resolves
  the same as a full id everywhere a `write` block takes one. Exports still carry full ids, so
  they stay restorable.
- `rows` no longer does `SELECT *`. Serializing one embedded `halfvec(4000)` column used to cost
  roughly **13,000 tokens for a single embedded chunk**; the same chunk now costs about **232**.
- Group fetch takes `members: "full"|"ids"|"count"`. On the largest group in a test vault,
  `"count"` mode cut the fetch from **735 tokens to 133**.
- `snippet` no longer raises a raw `InvalidTextRepresentation` error out of the tool on a
  malformed id — it returns a message instead.

### New in janitor
- Archiving a group cascades `lifecycle: archived` to its tasks in one statement, state-only (no
  cascaded task's `updated_at` moves, none flips to `doing`). Each cascaded task's edit row
  records `cascaded_from_group`, so a later unarchive can tell a task that came along for the
  ride from one archived deliberately.
- The duplicate-group report replaces two counts that had never once fired on real data with two
  capped, evidence-bearing lists: name-similarity (excluding numbered series like `grp-4`/`grp-6`)
  and content-overlap (Jaccard, not containment, restricted to same-type pairs). Report only —
  nothing here writes.

### Security
- Every route requires `Authorization: Bearer <token>`; tokens are a comma-separated list so a
  rotation can run old and new together, compared with `secrets.compare_digest`.
- The `sql` tool's debug gate moved server-side (`UPDATE settings SET value='true' WHERE
  key='debug'` on the backend) — a client-side setting can no longer turn it on by itself.
- Published ports default to `${KOVAULT_BIND:-127.0.0.1}`; the bundled embedder publishes no host
  port at all.
- `/kovault:setup` now probes the embedding endpoint's dimension from inside the container and
  refuses anything under 4000 dims, and finishes by writing a row and confirming it becomes
  searchable.

### Housekeeping
- Dead code removed: unused `_EMBEDDED_FIELDS`/`_embedded_field_changed`, and
  `set_updated_at_pages()` (kept only inside the migration pair, where the down migration needs
  it back).
- Every remaining reference to the janitor's `-freshness` flag is corrected — janitor's own
  docstring, its argument hint, both READMEs, the help listing. An unrecognized janitor flag is
  now named and rejected instead of silently running a plain diagnose.
- `write`'s docstring no longer points at `read_sql` (a tool that exists nowhere in either repo);
  it points at `rows`. `snippet`'s parameters now say its `ids:`/`titles:` are LISTS explicitly —
  a singular `id:` was silently ignored, indistinguishable from a genuine miss.
- `settings.md` (plugin repo) is corrected to say the local debug flag controls PostToolUse
  logging only, not the `sql` gate — that moved server-side, above.

`mcp-server/pyproject.toml` and `kovault_mcp.__version__` go to `1.5.0` with this release.
