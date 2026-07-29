"""B2: bearer auth in front of every route, and the server-side gate on the raw `sql` tool.

The middleware is driven as real ASGI — scope in, messages out — because "does an unauthenticated
request actually get a 401" is the only question that matters here, and a mocked-out version of it
would answer a different one.
"""
import asyncio
import json
import unittest
from contextlib import contextmanager

from kovault_mcp import server as sv
from kovault_mcp.config import Config
from tests.test_write_batch import FakeCursor

TOKEN = "s3cret-token-value"
OTHER = "second-token-for-rotation"

# every surface the middleware has to cover — /page-meta looks harmless and lists the id and
# modification time of every page in the vault
ROUTES = [("POST", "/mcp"), ("GET", "/export"), ("POST", "/relocate-sources"),
          ("GET", "/page-meta"), ("POST", "/debug-log")]


def scope(method="GET", path="/mcp", auth=None, type_="http"):
    headers = [(b"host", b"localhost")]
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return {"type": type_, "method": method, "path": path, "headers": headers}


class Sink:
    """Downstream app + send() collector."""

    def __init__(self):
        self.reached = False
        self.messages = []

    async def app(self, scope, receive, send):
        self.reached = True

    async def send(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return next((m["status"] for m in self.messages if m["type"] == "http.response.start"), None)

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages
                        if m["type"] == "http.response.body")

    @property
    def headers(self):
        start = next(m for m in self.messages if m["type"] == "http.response.start")
        return {k.decode().lower(): v.decode() for k, v in start["headers"]}


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


# bodies the two JSON routes need in order to reach their own logic rather than a parse error
BODIES = {"/relocate-sources": {"old_prefix": "/a", "new_prefix": "/b"},
          "/debug-log": {"tool": "lookup"}, "/mcp": {}}


class StubDB:
    """The least DB a route needs to run to completion. Whether the route does its job is covered
    elsewhere; here the only question is whether it was reached at all."""

    def query(self, sql, params=None):
        return []

    def query_one(self, sql, params=None):
        return None

    @contextmanager
    def connection(self):
        class Conn:
            @contextmanager
            def cursor(self):
                yield FakeCursor()

            def commit(self):
                pass

            def rollback(self):
                pass
        yield Conn()


def call(tokens, **kw) -> Sink:
    sink = Sink()
    mw = sv.BearerAuthMiddleware(sink.app, tokens)
    asyncio.run(mw(scope(**kw), _receive, sink.send))
    return sink


class TestTokenIdentity(unittest.TestCase):
    def test_a_valid_token_resolves_to_an_identity(self):
        self.assertEqual(sv.token_identity(TOKEN, [TOKEN]), sv.SHARED_TOKEN_USER)

    def test_any_member_of_the_list_matches(self):
        # rotation: add the new token, roll the clients, drop the old one — both work meanwhile
        for t in (TOKEN, OTHER):
            self.assertEqual(sv.token_identity(t, [TOKEN, OTHER]), sv.SHARED_TOKEN_USER)

    def test_a_wrong_or_empty_token_resolves_to_nobody(self):
        for bad in ("", "wrong", TOKEN + "x", TOKEN[:-1], TOKEN.upper(), " " + TOKEN):
            self.assertIsNone(sv.token_identity(bad, [TOKEN]), bad)

    def test_no_configured_tokens_means_nothing_matches(self):
        self.assertIsNone(sv.token_identity(TOKEN, []))
        self.assertIsNone(sv.token_identity("", []))

    def test_a_non_ascii_token_does_not_explode(self):
        # compare_digest refuses non-ASCII str; a secret file can hold anything
        self.assertEqual(sv.token_identity("pässwörd", ["pässwörd"]), sv.SHARED_TOKEN_USER)
        self.assertIsNone(sv.token_identity("pässwörd", [TOKEN]))

    def test_it_is_a_constant_time_comparison(self):
        # the one line in this release where the lazy version is actually wrong: `==` returns on
        # the first differing byte and leaks the token through timing
        import inspect
        src = inspect.getsource(sv.token_identity)
        self.assertIn("compare_digest", src)


class TestBearerHeader(unittest.TestCase):
    def test_extraction(self):
        self.assertEqual(sv.bearer_token(scope(auth=f"Bearer {TOKEN}")), TOKEN)
        self.assertEqual(sv.bearer_token(scope(auth=f"bearer {TOKEN}")), TOKEN)   # scheme is case-insensitive
        self.assertEqual(sv.bearer_token(scope(auth=f"  Bearer   {TOKEN}  ")), TOKEN)

    def test_no_header_or_wrong_scheme_is_empty(self):
        self.assertEqual(sv.bearer_token(scope()), "")
        self.assertEqual(sv.bearer_token(scope(auth=f"Basic {TOKEN}")), "")
        self.assertEqual(sv.bearer_token(scope(auth=TOKEN)), "")


class TestMiddleware(unittest.TestCase):
    def test_every_route_refuses_an_unauthenticated_request(self):
        for method, path in ROUTES:
            s = call([TOKEN], method=method, path=path)
            self.assertEqual(s.status, 401, path)
            self.assertFalse(s.reached, path)          # never reaches the handler

    def test_every_route_refuses_a_wrong_token(self):
        for method, path in ROUTES:
            s = call([TOKEN], method=method, path=path, auth="Bearer nope")
            self.assertEqual(s.status, 401, path)
            self.assertFalse(s.reached, path)

    def test_every_route_accepts_the_right_token(self):
        for method, path in ROUTES:
            s = call([TOKEN], method=method, path=path, auth=f"Bearer {TOKEN}")
            self.assertTrue(s.reached, path)
            self.assertEqual(s.messages, [])            # middleware wrote nothing itself

    def test_both_tokens_of_a_rotating_pair_work(self):
        for t in (TOKEN, OTHER):
            self.assertTrue(call([TOKEN, OTHER], auth=f"Bearer {t}").reached)

    def test_the_401_says_what_is_needed(self):
        s = call([TOKEN])
        self.assertEqual(s.headers["www-authenticate"], "Bearer")
        self.assertEqual(json.loads(s.body)["error"], "unauthorized")
        self.assertIn("Authorization: Bearer", json.loads(s.body)["detail"])

    def test_the_401_never_echoes_the_token(self):
        s = call([TOKEN], auth=f"Bearer {TOKEN}x")
        self.assertNotIn(TOKEN.encode(), s.body)

    def test_non_http_scopes_pass_through(self):
        # lifespan/websocket carry no headers; refusing them would break startup
        self.assertTrue(call([TOKEN], type_="lifespan").reached)

    def test_it_is_raw_asgi_and_never_wraps_the_body(self):
        # NOT BaseHTTPMiddleware: that buffers the response, which would break /mcp's streaming
        # transport and /export's zip. Raw ASGI hands `send` straight through untouched.
        from starlette.middleware.base import BaseHTTPMiddleware
        self.assertFalse(issubclass(sv.BearerAuthMiddleware, BaseHTTPMiddleware))
        sink = Sink()
        mw = sv.BearerAuthMiddleware(sink.app, [TOKEN])
        sent = []

        async def app(scope, receive, send):
            await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
        mw.app = app
        asyncio.run(mw(scope(auth=f"Bearer {TOKEN}"), _receive, lambda m: sent.append(m) or
                       asyncio.sleep(0)))
        self.assertEqual(sent, [{"type": "http.response.body", "body": b"chunk",
                                 "more_body": True}])   # verbatim, unbuffered


class TestAgainstTheRealApp(unittest.TestCase):
    """The unit tests above prove the middleware decides correctly. This one proves it is actually
    MOUNTED — that `mcp.run(middleware=…)` reaches the real FastMCP ASGI app and wraps every route,
    including /mcp itself. A wrong kwarg name or a route registered outside the stack would pass
    every other test in this file and ship an open server."""

    def setUp(self):
        try:
            import httpx                                  # noqa: F401  (a fastmcp dependency)
        except ImportError:                               # pragma: no cover
            self.skipTest("httpx not installed")
        self._db = sv._DB
        sv._DB = StubDB()          # enough DB for a route to finish; routes are not under test

    def tearDown(self):
        sv._DB = self._db

    def _run(self, headers):
        import httpx

        async def go():
            app = sv.mcp.http_app(middleware=sv.http_middleware([TOKEN]))
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://kovault") as c:
                    out = {}
                    for m, p in ROUTES:
                        r = await c.request(m, p, headers=headers, json=BODIES.get(p))
                        out[p] = r.status_code
                    return out
        return asyncio.run(go())

    def test_no_token_is_401_on_every_route(self):
        self.assertEqual(set(self._run({}).values()), {401})

    def test_wrong_token_is_401_on_every_route(self):
        self.assertEqual(set(self._run({"Authorization": "Bearer nope"}).values()), {401})

    def test_right_token_gets_past_the_gate_on_every_route(self):
        codes = self._run({"Authorization": f"Bearer {TOKEN}"})
        self.assertNotIn(401, codes.values(), codes)


class TestOpenMode(unittest.TestCase):
    def test_no_tokens_installs_no_middleware(self):
        self.assertEqual(sv.http_middleware([]), [])

    def test_tokens_install_exactly_one(self):
        mw = sv.http_middleware([TOKEN])
        self.assertEqual(len(mw), 1)
        self.assertIs(mw[0].cls, sv.BearerAuthMiddleware)

    def test_the_warning_names_every_exposed_surface(self):
        from kovault_mcp import main
        lines = []
        main.logging.getLogger("kovault_mcp").warning = lambda m, *a: lines.append(str(m) % a if a else str(m))
        main._warn_if_open([], Config())
        text = "\n".join(lines)
        for surface in ("/mcp", "/export", "/relocate-sources", "/page-meta", "/debug-log"):
            self.assertIn(surface, text)
        self.assertIn("WITHOUT AUTHENTICATION", text)
        self.assertIn("kovault_auth_token.txt", text)

    def test_the_warning_is_silent_when_a_token_is_set(self):
        from kovault_mcp import main
        lines = []
        main.logging.getLogger("kovault_mcp").warning = lambda m, *a: lines.append(m)
        main._warn_if_open([TOKEN], Config())
        self.assertEqual(lines, [])


class TestConfigTokens(unittest.TestCase):
    def test_comma_separated_with_whitespace(self):
        cfg = Config(auth_token=f" {TOKEN} , {OTHER} ,, ")
        self.assertEqual(cfg.auth_tokens, [TOKEN, OTHER])

    def test_empty_means_open(self):
        for raw in ("", "   ", ",,"):
            self.assertEqual(Config(auth_token=raw).auth_tokens, [])


class TestSqlGate(unittest.TestCase):
    """The gate moves server-side: a non-plugin client never runs the PreToolUse hook."""

    class DB:
        def __init__(self, debug):
            self.debug = debug

        def settings(self):
            return {"debug": self.debug}

    def setUp(self):
        self._db = sv._DB

    def tearDown(self):
        sv._DB = self._db

    def test_refused_when_debug_is_off(self):
        sv._DB = self.DB(False)
        out = sv.sql("SELECT 1")
        self.assertIn("sql is off", out)
        self.assertIn("key='debug'", out)          # says where to turn it on

    def test_refused_before_the_query_is_even_parsed(self):
        sv._DB = self.DB(False)
        self.assertIn("sql is off", sv.sql("DROP TABLE pages"))

    def test_reaches_the_read_only_checks_when_debug_is_on(self):
        sv._DB = self.DB(True)
        self.assertIn("only SELECT / WITH", sv.sql("DELETE FROM pages"))
        self.assertIn("read-only", sv.sql("WITH x AS (DELETE FROM pages) SELECT 1"))

    def test_default_settings_ship_it_off(self):
        from kovault_mcp.db import DEFAULT_SETTINGS
        self.assertIs(DEFAULT_SETTINGS["debug"], False)


if __name__ == "__main__":
    unittest.main()


class TestSqlGateFailsClosed(unittest.TestCase):
    """The `sql` gate must open only on the JSON boolean true. Every other setting in the table
    is an object, so {"enabled": false} is the shape an admin reaches for by analogy — under a
    truthiness test that would turn raw SQL access ON while they believed they had turned it off."""

    def _sql_with(self, value):
        class DB:
            def settings(self):
                return {"debug": value}
        old, sv._DB = sv._DB, DB()
        try:
            return sv.sql(query="SELECT 1")
        finally:
            sv._DB = old

    def test_bare_true_opens_it(self):
        self.assertNotIn("sql is off", self._sql_with(True))

    def test_bare_false_closes_it(self):
        self.assertIn("sql is off", self._sql_with(False))

    def test_missing_setting_closes_it(self):
        self.assertIn("sql is off", self._sql_with(None))

    def test_object_form_closes_it_however_it_reads(self):
        for v in ({"enabled": False}, {"enabled": True}, {}, "true", 1):
            self.assertIn("sql is off", self._sql_with(v), f"{v!r} must not open the gate")
