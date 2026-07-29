# Embedding service (standalone)

Generic OpenAI-compatible embedding endpoint (Ollama), kept in its **own** compose project so it
stays reusable. Kovault is one consumer; anything joining the shared `kovault-net` network can
point at the same `http://embedding:11434/v1` endpoint.

Kovault does not require *this* service. `kovault-mcp` calls whatever URL sits in the DB
`embedding` setting (see `../docker/02-init.sql`). This is a convenient default.

## The dimension requirement

**Model must emit at least 4000 dimensions.** Vector columns are `halfvec(4000)` and the client
MRL-truncates **down** only. A smaller model gives you a vault that accepts writes and never
becomes searchable, with no error anywhere, because the embed worker treats a bad endpoint as a
temporary blip and retries forever. `qwen3-embedding:8b` (4096) satisfies it.

## Option A — an endpoint you already run

Any `/v1/embeddings` server. Put its URL in the `embedding` setting (below). Must be reachable
*from inside the kovault-mcp container*: a service on `kovault-net` by name,
`http://host.docker.internal:11434` for something on this host, or a normal URL for another box.

## Option B — the bundled service

```bash
docker network create kovault-net   # once, shared with the kovault project — see ../README.md
                                     # -> "Create the shared network"
cd embedding
docker compose up -d
# pull an embedding model (>= 4000 output dims; the server MRL-truncates to 4000):
docker compose exec embedding ollama pull qwen3-embedding:8b
```

Ollama then serves the OpenAI-compatible API at `http://embedding:11434/v1`, reachable only from
containers on `kovault-net`. It publishes no host port. Ollama has no authentication of its own, so
a published port would hand your GPU and model store to anyone who can reach the machine. The
shared network gives `kovault-mcp` the access it needs without that exposure.

CPU-only host? Delete the `deploy.resources` block in `docker-compose.yml`.

## Point Kovault at it

Edit the row in `../docker/02-init.sql` **before first boot**. Afterwards:
`UPDATE settings SET value = '…' WHERE key = 'embedding';` then `/kovault:janitor -embed`.

```sql
('embedding', '{"model": "qwen3-embedding:8b", "endpoint": "http://embedding:11434", "dims": 4000}')
```

`endpoint` is the base URL; the server appends `/v1/embeddings`. Leave `dims` at 4000, the width
the client truncates to, and it must match the `halfvec(N)` in the schema. Default
`http://embedding:11434` reaches this service over `kovault-net`, no host port involved.

## Changing the model later

Re-point the `embedding` setting, keep `dims` at 4000 to match the `halfvec(N)`, then run
`/kovault:janitor -embed` to re-embed everything. A model emitting fewer than 4000 dims cannot be
used without a schema change and a full re-ingest.
