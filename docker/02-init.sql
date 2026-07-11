-- Second init script (runs after 01-schema.sql on first boot). Seeds server settings.
-- Admin-tunable later via SQL or a future admin flow.

-- Graph — ONLY if Apache AGE is re-added (BUILD.md B5; default build uses a recursive CTE):
-- LOAD 'age';
-- SET search_path = ag_catalog, "$user", public;
-- SELECT create_graph('kovault');   -- projected from links

-- Default server settings. Set the `embedding` endpoint/model to a real OpenAI-compatible
-- embedding endpoint before ingesting (e.g. the standalone ../embedding/ service, an existing
-- Ollama, or a box elsewhere). Default reaches the host at :11434 via host.docker.internal.
-- Swap models by repointing here, then run /janitor -embed to re-embed everything.
INSERT INTO settings (key, value) VALUES
  ('rrf_k',          '60'),
  ('ladder_chunks',  '{"r": 0.70, "floor": 3, "cap": 9}'),
  ('ladder_pages',   '{"r": 0.75, "floor": 1, "cap": 6}'),
  ('freshness_days', '{"hot": 30, "warm": 90}'),
  ('freshness_auto',  '{"enabled": true, "cooldown_seconds": 3600}'),
  ('embedding',      '{"model": "kovault-embed", "endpoint": "http://host.docker.internal:11434", "dims": 4000}')
ON CONFLICT (key) DO NOTHING;
