"""B1: the deployment shape, checked against the files that define it.

Config drift is invisible until it costs a day. `http://embedding:8080` sat in DEFAULT_SETTINGS
for months pointing at nothing, and a severed embedder never raises — the worker treats it as a
temporary blip and retries forever. So the two places that define the endpoint are compared to
each other here, and the compose files are read for what they actually publish.
"""
import json
import re
import unittest
from pathlib import Path

import yaml

from kovault_mcp.db import DEFAULT_SETTINGS

DOCKER = Path(__file__).resolve().parents[2] / "docker"
EMBEDDING = Path(__file__).resolve().parents[2] / "embedding"
NET = "kovault-net"


def seeded_settings() -> dict:
    """The rows 02-init.sql seeds, as Python — `('key', 'json literal')` pairs."""
    sql = (DOCKER / "02-init.sql").read_text()
    body = sql.split("INSERT INTO settings (key, value) VALUES", 1)[1].split("ON CONFLICT", 1)[0]
    return {k: json.loads(v) for k, v in re.findall(r"\(\s*'([^']+)'\s*,\s*'(.*?)'\s*\)", body)}


def compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


class TestSettingsDoNotDrift(unittest.TestCase):
    def test_every_seeded_setting_matches_the_python_fallback(self):
        seeded = seeded_settings()
        for key, value in seeded.items():
            self.assertIn(key, DEFAULT_SETTINGS, f"02-init.sql seeds '{key}', db.py has no fallback")
            self.assertEqual(DEFAULT_SETTINGS[key], value, f"'{key}' differs between the two")

    def test_the_fallback_adds_nothing_the_schema_does_not_seed(self):
        self.assertEqual(set(DEFAULT_SETTINGS) - set(seeded_settings()), set())

    def test_the_embedding_endpoint_is_the_shared_network_one(self):
        # not host.docker.internal (the embedder no longer publishes a host port) and not the
        # long-dead :8080
        emb = DEFAULT_SETTINGS["embedding"]
        self.assertEqual(emb["endpoint"], f"http://embedding:11434")
        self.assertEqual(emb["dims"], 4000)          # must equal the halfvec(N) in 01-schema.sql

    def test_the_schema_width_matches_the_configured_dims(self):
        schema = (DOCKER / "01-schema.sql").read_text()
        widths = set(re.findall(r"halfvec\((\d+)\)", schema))
        self.assertEqual(widths, {str(DEFAULT_SETTINGS["embedding"]["dims"])})

    def test_debug_ships_off(self):
        self.assertIs(seeded_settings()["debug"], False)


class TestExposure(unittest.TestCase):
    """Nothing reachable off this machine until the operator says so."""

    def published(self, path: Path) -> list[str]:
        return [p for svc in compose(path)["services"].values() for p in (svc.get("ports") or [])]

    def test_every_published_port_binds_to_kovault_bind(self):
        ports = self.published(DOCKER / "docker-compose.yml")
        self.assertTrue(ports)
        for p in ports:
            self.assertTrue(str(p).startswith("${KOVAULT_BIND:-127.0.0.1}:"), p)

    def test_the_default_is_loopback(self):
        for p in self.published(DOCKER / "docker-compose.yml"):
            self.assertIn(":-127.0.0.1}", str(p))

    def test_the_embedder_publishes_nothing(self):
        # Ollama has no authentication: a host port would hand the GPU and model store to anyone
        # who can reach the machine, and buys nothing the shared network does not already give.
        self.assertEqual(self.published(EMBEDDING / "docker-compose.yml"), [])

    def test_the_env_example_ships_loopback(self):
        self.assertIn("KOVAULT_BIND=127.0.0.1", (DOCKER / ".env.example").read_text())


class TestSharedNetwork(unittest.TestCase):
    def test_both_projects_join_it_and_neither_owns_it(self):
        for path in (DOCKER / "docker-compose.yml", EMBEDDING / "docker-compose.yml"):
            doc = compose(path)
            self.assertEqual(doc["networks"][NET], {"external": True}, path.parent.name)
            for name, svc in doc["services"].items():
                self.assertIn(NET, svc.get("networks") or [], f"{path.parent.name}:{name}")

    def test_the_endpoint_names_the_embedding_service(self):
        # http://embedding:11434 only resolves because compose aliases the SERVICE name on the
        # shared network — renaming the service silently severs embedding
        host = DEFAULT_SETTINGS["embedding"]["endpoint"].split("//")[1].split(":")[0]
        self.assertIn(host, compose(EMBEDDING / "docker-compose.yml")["services"])


class TestSecretsAreDeclared(unittest.TestCase):
    def test_both_secret_files_have_an_example(self):
        doc = compose(DOCKER / "docker-compose.yml")
        for name, spec in doc["secrets"].items():
            self.assertTrue((DOCKER / spec["file"]).with_suffix(".txt.example").exists()
                            or Path(str(DOCKER / spec["file"]) + ".example").exists(), name)


if __name__ == "__main__":
    unittest.main()
