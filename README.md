# Kovault backend

Self-hosted backend for Kovault. Holds Postgres and the DB credentials. ParadeDB (PostgreSQL 16 +
pgvector + pg_search + pg_trgm) plus the **kovault-mcp** server that the
[Kovault plugin](https://github.com/CarloHalman/kovault) talks to. Hybrid semantic + keyword +
graph index. Install once on your server, point plugin at its HTTP endpoint.

```bash
git clone https://github.com/CarloHalman/kovault-backend
cd kovault-backend
```

## Prerequisites

- **Docker + Docker Compose.**
- **Embedding endpoint** reachable from the `kovault-mcp` container, OpenAI-compatible
  **`/v1/embeddings`**, model emitting **at least 4000 dimensions** (step 3). Bundled
  [`embedding/`](embedding/) service or your own.
- **Windows, Docker Desktop:** the daemon must be able to see the directory you clone into. Where
  it cannot, the secret mounts in step 5 fail with `bind source path does not exist`, naming a file
  that is sitting right there on disk.

## 1. Create the shared network

Both compose projects join it, so `kovault-mcp` reaches the embedder without publishing a port for
it. Once:

```bash
docker network create kovault-net
```

## 2. Set the secrets

Steps 2, 4, 5 and 6 all run in `docker/`:

```bash
cd docker
```

**Generate both files. Do not copy the `.example` ones into place** — they hold the literal
placeholders `change-me-before-production` and `change-me-to-a-long-random-string`, and a stack
started on those is a stack anyone who reads this page can open:

```bash
openssl rand -base64 32 > secrets/kovault_db_password.txt
openssl rand -base64 32 > secrets/kovault_auth_token.txt
```

Auth token guards **every** route: tool surface, zip export, everything. Empty file runs without
authentication. Server starts either way and says loudly at every boot which of the two you chose.
**File must exist**, or compose refuses to start.

## 3. Embedding

Needs an OpenAI-compatible `/v1/embeddings` endpoint, model emitting **at least 4000 dimensions**.
Bundled service or your own.

Setup, models, the `embedding` setting, why the bundled one publishes no host port:
**[`embedding/README.md`](embedding/README.md)**. Its steps start from the repo root, so `cd ..`
first and `cd docker` again when you come back for step 4.

## 4. Choose what the ports are exposed to

```bash
cp .env.example .env
```

Binds to **`127.0.0.1` by default, this machine only**. To reach the vault from another machine,
set `KOVAULT_BIND` in `.env` to a specific interface address (VPN or LAN address, far better than
`0.0.0.0`), and **set an auth token first**. Past loopback, anyone who can route to the port can
read and write the entire vault and download it as a zip.

## 5. Build and run Kovault

```bash
docker compose up --build
```

Brings up the `kovault` project (kovault-db + kovault-mcp, grouped). First boot runs `01-schema.sql`
then `02-init.sql` automatically. DB image is `paradedb/paradedb` (pgvector + pg_search prebuilt);
pg_trgm is stock contrib.

Second stack on the same host? `docker compose -p <other-name> up` — container names come from the
project, so nothing collides. One trap: compose **appends** to `ports:` when merging `-f` files, it
does not replace, so an override that moves a port leaves the original binding in place and the
container tries to bind both. Tag the key `ports: !override` to replace.

## 6. Verify

```bash
docker compose exec kovault-db psql -U kovault -d kovault -c "\dx"            # vector + pg_search + pg_trgm
docker compose exec kovault-db psql -U kovault -d kovault -c "SELECT key FROM settings;"
```

MCP server now serves at **`http://127.0.0.1:8000/mcp`** (or whatever `KOVAULT_BIND` says). Give
that URL to the plugin's `/kovault:setup`, which finishes the job: writes a row and confirms it
becomes searchable, the only thing proving the embedder is wired up.

## 7. Identity headers

Server reads `X-Kovault-User` / `X-Kovault-Actor` HTTP headers (plugin's `/kovault:setup` sets
these) to stamp edits. Fallbacks: `KOVAULT_DEFAULT_USER` / `KOVAULT_DEFAULT_ACTOR` env on the
`kovault-mcp` service.

## Upgrading

Your version is `version` in `mcp-server/pyproject.toml`. Read
[`CHANGELOG.md`](CHANGELOG.md): one section per release, with
breaking changes and migration steps. Read **every** section between your version and this one, not
only the newest, migrations stack.

Raw diff instead: releases are tagged, so `git diff v1.4.1..v1.5.0` (or GitHub's compare view) shows
everything that changed.

### Restoring a backup taken before 1.5.2

Dumps from 1.5.1 and earlier do not restore with a plain `pg_restore`. `f_unaccent` shipped without
a pinned `search_path`, `pg_restore` runs with an empty one, and every table with a `*_norm`
generated column fails to create. The dump is fine; the restore is what breaks.

Run the migration against the empty target **first**, then restore into it:

```bash
psql "$KOVAULT_DSN" -f docker/migrate_1.5.2.sql   # on the new, empty database
pg_restore -U kovault -d kovault your-backup.dump
```

The dump's own `CREATE FUNCTION f_unaccent` then fails with `already exists`, `pg_restore` carries
on, and the fixed function is the one the generated columns use. Run `migrate_1.5.2.sql` on your
live database too, so dumps you take from now on restore without the dance.

## Notes

- **BM25 (pg_search)** is the one API varying across ParadeDB releases. Keyword search errors:
  confirm the `USING bm25 (...) WITH (key_field='id')` indexes and the `col @@@ 'terms'` /
  `paradedb.score(id)` calls against your pinned pg_search version. Vector + graph are stock.
- Only `kovault-mcp` holds DB credentials. Users' plugins only ever talk to its HTTP endpoint.
