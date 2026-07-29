-- Second init script (runs after 01-schema.sql on first boot). Seeds server settings.
-- Admin-tunable later via SQL or a future admin flow.

-- Graph — ONLY if Apache AGE is re-added (BUILD.md B5; default build uses a recursive CTE):
-- LOAD 'age';
-- SET search_path = ag_catalog, "$user", public;
-- SELECT create_graph('kovault');   -- projected from links

-- Default server settings. The `embedding` endpoint must be an OpenAI-compatible /v1/embeddings
-- server the kovault-mcp CONTAINER can reach. The default is the bundled ../embedding/ service
-- over the shared kovault-net — no host port, so nothing else on the network can use your GPU.
-- Point it anywhere else (an Ollama you already run, another box) and it just works, as long as
-- the model emits AT LEAST 4000 dimensions: the schema is halfvec(4000) and the client only
-- MRL-truncates DOWN, so a smaller model produces a vault that accepts writes and never becomes
-- searchable. Swap models by repointing here, then /janitor -embed to re-embed everything.
-- Keep this row in step with DEFAULT_SETTINGS in mcp-server/kovault_mcp/db.py.
INSERT INTO settings (key, value) VALUES
  ('rrf_k',          '60'),
  ('ladder_chunks',  '{"r": 0.70, "floor": 3, "cap": 9}'),
  ('ladder_pages',   '{"r": 0.75, "floor": 1, "cap": 6}'),
  ('embedding',      '{"model": "qwen3-embedding:8b", "endpoint": "http://embedding:11434", "dims": 4000}'),
  -- Background embed worker. Seeded so an admin can actually see and tune it; it used to exist
  -- only as a Python fallback, which meant the knobs were invisible from the settings table.
  ('embed_worker',   '{"enabled": true, "poll_seconds": 3, "batch": 32, "max_retries": 3}'),
  -- Server-side gate for the raw read-only `sql` tool (B2). OFF by default: it is a debugging
  -- aid, and the gate has to live here rather than in a client hook a non-plugin caller skips.
  ('debug',          'false')
ON CONFLICT (key) DO NOTHING;
