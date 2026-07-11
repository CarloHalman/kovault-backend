# Embedding service (standalone)

A generic OpenAI-compatible embedding endpoint (Ollama), kept in its **own** compose project so
it is clearly reusable — Kovault is just one consumer; anything else on the host can use the same
`http://<host>:11434/v1` endpoint too.

Kovault does not require *this* service specifically. `kovault-mcp` calls whatever URL is in the
DB `embedding` setting (see `../docker/02-init.sql`); this is simply a convenient default.

## Run

```bash
cd embedding
docker compose up -d
# pull an embedding model (>= 4000 output dims; the server MRL-truncates to 4000):
docker compose exec embedding ollama pull qwen3-embedding:8b
```

Ollama then serves the OpenAI-compatible API at `http://<host>:11434/v1`.

## Point Kovault at it

In `../docker/02-init.sql` (before first boot) or via the `settings` table after boot, set the
`embedding` row's `model`/`endpoint`/`dims` to match. Default endpoint is
`http://host.docker.internal:11434`, which resolves to this service published on the host.

CPU-only host? Delete the `deploy.resources` block in `docker-compose.yml`.
