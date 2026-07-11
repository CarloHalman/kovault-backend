# Kovault backend

Self-hosted backend for Kovault, holds the Postgres database and the DB credentials. ParadeDB
(PostgreSQL 16 + pgvector + pg_search + pg_trgm) plus the **kovault-mcp** server that the
[Kovault plugin](https://github.com/CarloHalman/kovault) talks to; a hybrid semantic + keyword +
graph index. Install this once on your server, then point the plugin at its HTTP endpoint.

```bash
git clone https://github.com/CarloHalman/kovault-backend
cd kovault-backend
```

## Prerequisites

- **Docker + Docker Compose.**
- **An embedding endpoint** the `kovault-mcp` container can reach, exposing an **OpenAI-compatible
  `/v1/embeddings`** API. Either:
  - Run the standalone [`embedding/`](embedding/) service in this repo (its own compose project,
    reusable by other apps) — see step 3 below, or
  - Point at any existing OpenAI-compatible endpoint (an Ollama you already run, a dedicated
    **Qwen3-Embedding-8B** server MRL-projected to **4000** dims, another box, …).

## 1. Set the DB password

```bash
cd docker
cp secrets/kovault_db_password.txt.example secrets/kovault_db_password.txt
# edit secrets/kovault_db_password.txt to a real password
```

## 2. Match the vector dimension to your embedding model

The embedding **columns are fixed at schema creation**, so `halfvec(N)` must equal your model's
output dim. Default is `halfvec(4000)` (Qwen3-8B MRL). If you use `qwen3-embedding:4b` (1024):

```bash
sed -i 's/halfvec(4000)/halfvec(1024)/g' docker/01-schema.sql   # do this BEFORE first boot
```

Then set the endpoint/model/dims that the server calls, in `docker/02-init.sql`:

```sql
('embedding', '{"model": "qwen3-embedding:4b", "endpoint": "http://<embed-host>:11434/v1", "dims": 1024}')
```

(`endpoint` is the base URL; the server calls `<endpoint>/v1/embeddings`. The default
`http://host.docker.internal:11434` reaches an embedding published on the host at :11434. `dims`
must match the `halfvec(N)` you set.)

## 3. Embedding — point at your own, or run one alongside

Kovault needs an OpenAI-compatible embedding endpoint. **Choose one:**

- **(A) Use an embedding you already run** — an existing Ollama, a shared box, any
  `/v1/embeddings` server. Just set its URL in step 2; skip the rest of this step.
- **(B) Run the bundled one alongside Kovault** — it lives in its **own** compose project
  (`embedding/`), on purpose, so it stays reusable by other apps too:

  ```bash
  cd embedding
  docker compose up -d
  docker compose exec embedding ollama pull qwen3-embedding:8b   # a model >= 4000 output dims
  ```

Either way, the endpoint you chose goes in the `embedding` setting (step 2). Default
`http://host.docker.internal:11434` reaches an embedding published on the host at :11434.

## 4. Build and run Kovault

```bash
cd docker
docker compose up --build
```

Brings up the `kovault` project (kovault-db + kovault-mcp, grouped together). First boot runs
`01-schema.sql` then `02-init.sql` automatically. The DB image is `paradedb/paradedb` (pgvector +
pg_search prebuilt); pg_trgm is stock contrib.

## 5. Verify

```bash
docker compose exec kovault-db psql -U kovault -d kovault -c "\dx"            # vector + pg_search + pg_trgm
docker compose exec kovault-db psql -U kovault -d kovault -c "SELECT key FROM settings;"
```

The MCP server is now serving at **`http://<your-host>:8000/mcp`**. That URL is what you give
the plugin's `/setup-kovault`.

## 6. Identity headers

The server reads `X-Kovault-User` / `X-Kovault-Actor` HTTP headers (the plugin's `/setup-kovault`
sets these) to stamp edits. You can set fallbacks with `KOVAULT_DEFAULT_USER` /
`KOVAULT_DEFAULT_ACTOR` env on the `kovault-mcp` service.

## Notes

- **Change the embedding model later?** Re-point the `embedding` setting, keep `dims` matching
  the `halfvec(N)`, then run `/janitor-kovault -embed` to re-embed everything. Changing dims
  means a schema change + full re-ingest.
- **BM25 (pg_search)** is the one API that varies across ParadeDB releases; if keyword search
  errors, confirm the `USING bm25 (...) WITH (key_field='id')` indexes and the `col @@@ 'terms'`
  / `paradedb.score(id)` calls against your pinned pg_search version. Vector + graph are stock.
- Only `kovault-mcp` holds DB credentials; users' plugins only ever talk to its HTTP endpoint.
