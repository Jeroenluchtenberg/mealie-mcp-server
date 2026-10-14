"""Startup and transport checks without a live Mealie instance."""

import json
import logging
import runpy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest


# load_server patches httpx.Client; keep the real class to build mock clients.
RealClient = httpx.Client


class BuiltClients(list):
    """Every HTTP client the server created; closed once all of them are."""

    @property
    def is_closed(self):
        return bool(self) and all(client.is_closed for client in self)


def paths(requests):
    return [request.url.path for request in requests]


def authentik_user(username, token="user-token", active=True):
    return {
        "username": username,
        "is_active": active,
        "attributes": {"mealie_token": token} if token else {},
    }


@pytest.fixture
def load_server(monkeypatch, request):
    monkeypatch.setenv("MEALIE_BASE_URL", "http://mealie.invalid")
    monkeypatch.setenv("AUTHENTIK_URL", "http://authentik.invalid")
    monkeypatch.setenv("AUTHENTIK_TOKEN", "service-placeholder")
    for name in (
        "MEALIE_API_KEY", "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT",
        "MEALIE_ENABLE_AI_IMPORT", "MCP_USER_HEADER", "AUTHENTIK_TOKEN_ATTRIBUTE",
        "MEALIE_TOKEN_CACHE_TTL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)

    def load(health_status=200, authentik_status=200, users=None):
        requests = []
        clients = BuiltClients()

        def respond(api_request):
            requests.append(api_request)
            path = api_request.url.path
            if api_request.url.host == "authentik.invalid":
                if path == "/api/v3/core/users/me/":
                    return httpx.Response(authentik_status, json={})
                assert path == "/api/v3/core/users/"
                username = api_request.url.params["username"]
                found = users if users is not None else [authentik_user(username)]
                return httpx.Response(200, json={"results": found})
            if path == "/api/app/about":
                return httpx.Response(health_status, json={})
            assert path == "/api/recipes"
            return httpx.Response(200, json={"items": [], "total": 0})

        def build(**kwargs):
            kwargs.pop("transport", None)
            client = RealClient(**kwargs, transport=httpx.MockTransport(respond))
            clients.append(client)
            request.addfinalizer(client.close)
            return client

        factory = Mock(side_effect=build)
        monkeypatch.setattr("httpx.Client", factory)
        namespace = runpy.run_module("server", run_name="transport_test_server")
        return namespace, clients, requests, factory

    return load


async def test_import_and_discovery_need_no_configuration_or_network(load_server, monkeypatch):
    monkeypatch.delenv("MEALIE_BASE_URL")
    monkeypatch.delenv("AUTHENTIK_TOKEN")
    dotenv = Mock()
    monkeypatch.setattr("dotenv.load_dotenv", dotenv)
    server, _, requests, factory = load_server()
    assert isinstance(server["mcp"], server["FastMCP"])
    assert await server["mcp"].list_tools()
    assert await server["mcp"].list_prompts()
    assert requests == []
    factory.assert_not_called()
    dotenv.assert_not_called()


def test_sdk_import_server_discovery_is_lazy(load_server, monkeypatch):
    from mcp.cli.cli import _import_server

    monkeypatch.delenv("MEALIE_BASE_URL")
    monkeypatch.delenv("AUTHENTIK_TOKEN")
    _, _, requests, factory = load_server()
    discovered = _import_server(Path("src/server.py").resolve())
    assert discovered.name == "mealie"
    factory.assert_not_called()
    assert requests == []


def test_script_entry_point_uses_main_without_import_time_http(load_server):
    server, client, requests, factory = load_server()
    assert requests == []
    with patch.object(server["FastMCP"], "run", autospec=True) as run:
        runpy.run_path("src/server.py", run_name="__main__")
    run.assert_called_once()
    assert run.call_args.kwargs["transport"] == "streamable-http"
    assert "/api/app/about" in paths(requests)
    assert client.is_closed
    assert factory.call_count == 2


@pytest.mark.parametrize("transport", [None, "sse", "streamable-http"])
def test_transport_selection(load_server, monkeypatch, transport):
    if transport:
        monkeypatch.setenv("MCP_TRANSPORT", transport)
        monkeypatch.setenv("MCP_HOST", "0.0.0.0")
        monkeypatch.setenv("MCP_PORT", "8766")

    server, client, requests, factory = load_server()
    with patch.object(server["FastMCP"], "run", autospec=True) as run:
        server["main"]()
    mcp = run.call_args.args[0]
    assert mcp.settings.host == ("0.0.0.0" if transport else "127.0.0.1")
    assert mcp.settings.port == (8766 if transport else 8765)
    run.assert_called_once_with(
        mcp, transport=transport or "streamable-http", mount_path=None
    )
    assert "/api/app/about" in paths(requests)
    assert factory.call_count == 2
    assert client.is_closed


@contextmanager
def as_user(username, header="X-User"):
    """Run code as if inside an MCP request forwarded by the gateway."""
    from mcp.server.lowlevel.server import request_ctx

    token = request_ctx.set(
        SimpleNamespace(request=SimpleNamespace(headers={header: username}))
    )
    try:
        yield
    finally:
        request_ctx.reset(token)


def server_lifespan(mcp, transport):
    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    return app.router.lifespan_context(app)


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_startup_rejects_failed_health_check_and_closes(load_server, transport):
    server, client, requests, factory = load_server(health_status=503)
    with pytest.raises(httpx.HTTPStatusError):
        async with server_lifespan(server["mcp"], transport):
            pytest.fail("Unhealthy startup must not enter the running lifespan")
    assert "/api/app/about" in paths(requests)
    assert client.is_closed
    assert factory.call_count == 2


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_lifespan_closes_on_normal_shutdown(load_server, transport):
    server, client, requests, factory = load_server()
    async with server_lifespan(server["mcp"], transport):
        assert not client.is_closed
        assert "/api/app/about" in paths(requests)
    assert client.is_closed
    assert factory.call_count == 2


async def test_lifespan_closes_when_running_server_fails(load_server):
    server, client, _, _ = load_server()
    with pytest.raises(RuntimeError, match="synthetic failure"):
        async with server["mcp"].settings.lifespan(server["mcp"]):
            raise RuntimeError("synthetic failure")
    assert client.is_closed


async def test_http_client_survives_individual_mcp_sessions(load_server):
    server, client, _, factory = load_server()
    mcp = server["mcp"]
    async with server_lifespan(mcp, "streamable-http"):
        for _ in range(2):
            async with mcp.settings.lifespan(mcp):
                assert not client.is_closed
            assert not client.is_closed
        assert factory.call_count == 2
    assert client.is_closed


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MEALIE_BASE_URL", ""),
        ("AUTHENTIK_URL", ""),
        ("AUTHENTIK_TOKEN", ""),
        ("MEALIE_API_KEY", "legacy-shared-key"),
        ("MEALIE_API_KEY", ""),
        ("MCP_TRANSPORT", "stdio"),
        ("MCP_TRANSPORT", "unsupported"),
        ("MCP_USER_HEADER", " "),
        ("AUTHENTIK_TOKEN_ATTRIBUTE", ""),
        ("MEALIE_TOKEN_CACHE_TTL", "soon"),
        ("MEALIE_TOKEN_CACHE_TTL", "-1"),
        ("MEALIE_TOKEN_CACHE_TTL", "86400"),
        ("MCP_PORT", "not-a-port"),
        ("MCP_PORT", "0"),
        ("MCP_PORT", "65536"),
        ("MEALIE_ENABLE_AI_IMPORT", "yes"),
        ("MEALIE_ENABLE_AI_IMPORT", ""),
    ],
)
def test_invalid_configuration_fails_before_http_allocation(load_server, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    server, _, requests, factory = load_server()
    with pytest.raises(ValueError):
        server["main"]()
    assert requests == []
    factory.assert_not_called()


@pytest.mark.parametrize("value,enabled", [("true", True), ("TRUE", True), ("false", False), ("False", False)])
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_ai_import_lazy_runtime_registration(load_server, monkeypatch, value, enabled, transport):
    monkeypatch.setenv("MEALIE_ENABLE_AI_IMPORT", value)
    namespace, _, requests, factory = load_server()
    mcp = namespace["mcp"]
    assert len(await mcp.list_tools()) == 61
    factory.assert_not_called()
    assert requests == []
    async with server_lifespan(mcp, transport):
        tools = await mcp.list_tools()
        assert len(tools) == (62 if enabled else 61)
        assert ("import_recipe_with_ai" in {t.name for t in tools}) is enabled
        async with mcp.settings.lifespan(mcp):
            assert len(await mcp.list_tools()) == len(tools)
    assert factory.call_count == 2


@pytest.mark.parametrize("enabled", [False, True])
async def test_ai_import_explicit_configuration_discovery(load_server, enabled):
    namespace, _, requests, factory = load_server()
    config = namespace["ServerConfig"](
        "http://mealie.invalid",
        "http://authentik.invalid",
        "placeholder",
        enable_ai_import=enabled,
    )
    mcp = namespace["create_server"](config)
    assert len(await mcp.list_tools()) == (62 if enabled else 61)
    factory.assert_not_called()
    assert requests == []


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_invalid_ai_opt_in_fails_lazy_startup(load_server, monkeypatch, transport):
    monkeypatch.setenv("MEALIE_ENABLE_AI_IMPORT", "invalid")
    namespace, _, requests, factory = load_server()
    with pytest.raises(ValueError, match="MEALIE_ENABLE_AI_IMPORT"):
        async with server_lifespan(namespace["mcp"], transport):
            pytest.fail("Invalid opt-in must fail startup")
    factory.assert_not_called()
    assert requests == []


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_ai_import_main_registration(load_server, monkeypatch, transport):
    monkeypatch.setenv("MEALIE_ENABLE_AI_IMPORT", "true")
    monkeypatch.setenv("MCP_TRANSPORT", transport)
    namespace, _, _, _ = load_server()
    with patch.object(namespace["FastMCP"], "run", autospec=True) as run:
        namespace["main"]()
    mcp = run.call_args.args[0]
    assert len(await mcp.list_tools()) == 62


@pytest.mark.parametrize(
    ("value", "expected"), [("debug", "DEBUG"), ("WARN", "WARNING"), ("root", "INFO"), ("NOTSET", "INFO")]
)
def test_log_level_is_applied_with_existing_handlers(load_server, monkeypatch, value, expected):
    monkeypatch.setenv("LOG_LEVEL", value)
    server, _, _, _ = load_server()
    with patch.object(server["FastMCP"], "run"):
        server["main"]()
    assert server["logger"].getEffectiveLevel() == getattr(server["logging"], expected)


async def test_debug_configuration_does_not_log_http_urls_or_queries(load_server, monkeypatch, caplog):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    server, _, requests, _ = load_server()
    mcp = server["mcp"]
    with caplog.at_level(logging.DEBUG):
        async with server_lifespan(mcp, "streamable-http"):
            with as_user("private-user-marker"):
                mcp.client_provider._handle_request(
                    "GET", "/api/recipes", params={"search": "private-query-marker"}
                )
            assert server["logger"].getEffectiveLevel() == logging.DEBUG
            assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
            assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert "private-query-marker" in str(requests[-1].url)
    assert "private-query-marker" not in caplog.text
    assert "mealie.invalid" not in caplog.text
    assert "private-user-marker" not in caplog.text
    assert "user-token" not in caplog.text
    assert "HTTP Request:" not in caplog.text


def test_transport_failure_propagates_without_logging_exception_content(load_server, caplog):
    server, client, _, _ = load_server()
    with patch.object(server["FastMCP"], "run", side_effect=ValueError("Invalid transport")):
        with pytest.raises(ValueError, match="Invalid transport"):
            server["main"]()
    assert "Invalid transport" not in caplog.text
    assert "ValueError" in caplog.text
    assert client.is_closed


def test_main_health_failure_raises_before_http_runner(load_server, monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    server, client, _, _ = load_server(health_status=503)
    with patch.object(server["FastMCP"], "run") as run:
        with pytest.raises(httpx.HTTPStatusError):
            server["main"]()
    run.assert_not_called()
    assert client.is_closed


async def call_over_http(app, user_headers, sessions=2):
    """Open MCP sessions over streamable HTTP and call get_recipes in each."""
    results = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
    ) as client:
        for index in range(sessions):
            headers = {
                "Accept": "application/json, text/event-stream",
                **user_headers[index % len(user_headers)],
            }
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert response.status_code == 200
            assert '"name":"mealie"' in response.text
            headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
            response = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert response.status_code == 202
            response = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert response.status_code == 200
            tools_listing = response.text
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_recipes", "arguments": {}},
                },
            )
            assert response.status_code == 200
            event = next(
                line[6:] for line in response.text.splitlines() if line.startswith("data: ")
            )
            results.append((tools_listing, json.loads(event)))
    return results


@pytest.fixture
def http_app(load_server, monkeypatch):
    # sse-starlette's global event must not retain a previous test's event loop.
    monkeypatch.setattr("sse_starlette.sse.AppStatus.should_exit_event", None)

    def make(**kwargs):
        server, clients, requests, factory = load_server(**kwargs)
        return server["mcp"].streamable_http_app(), clients, requests, factory

    return make


@pytest.mark.parametrize("enable_ai_import", [False, True])
async def test_http_sessions_act_as_the_gateway_user(http_app, monkeypatch, enable_ai_import):
    monkeypatch.setenv("MEALIE_ENABLE_AI_IMPORT", str(enable_ai_import).lower())
    app, clients, requests, factory = http_app()

    async with app.router.lifespan_context(app):
        results = await call_over_http(app, [{"X-User": "alice"}], sessions=2)

    for tools_listing, result in results:
        assert "parse_ingredients" in tools_listing
        assert ('"name":"import_recipe_with_ai"' in tools_listing) is enable_ai_import
        assert "error" not in result
        assert result["result"].get("isError") is not True
    mealie_calls = [r for r in requests if r.url.path == "/api/recipes"]
    assert [r.headers["authorization"] for r in mealie_calls] == ["Bearer user-token"] * 2
    lookups = [r for r in requests if r.url.path == "/api/v3/core/users/"]
    # the second session reuses the cached token
    assert len(lookups) == 1
    assert lookups[0].url.params["username"] == "alice"
    assert lookups[0].url.params["is_active"] == "true"
    assert lookups[0].headers["authorization"] == "Bearer service-placeholder"
    assert paths(requests).count("/api/app/about") == 1
    assert factory.call_count == 2
    assert clients.is_closed


async def test_each_gateway_user_gets_their_own_token(http_app):
    users = [
        authentik_user("alice", token="alice-token"),
        authentik_user("bob", token="bob-token"),
    ]
    app, _, requests, _ = http_app(users=users)
    async with app.router.lifespan_context(app):
        await call_over_http(app, [{"X-User": "alice"}, {"X-User": "bob"}], sessions=2)
    mealie_calls = [r for r in requests if r.url.path == "/api/recipes"]
    assert [r.headers["authorization"] for r in mealie_calls] == [
        "Bearer alice-token",
        "Bearer bob-token",
    ]


@pytest.mark.parametrize(
    "user_headers,users,message",
    [
        ({}, None, "No authenticated user on this request"),
        ({"X-User": "  "}, None, "No authenticated user on this request"),
        ({"X-User": "carol"}, [], "No Mealie token linked to your account"),
        (
            {"X-User": "carol"},
            [authentik_user("carol", token=None)],
            "No Mealie token linked to your account",
        ),
        (
            {"X-User": "carol"},
            [authentik_user("carol", active=False)],
            "No Mealie token linked to your account",
        ),
        (
            {"X-User": "carol"},
            [authentik_user("carolyn")],
            "No Mealie token linked to your account",
        ),
    ],
)
async def test_missing_credentials_return_clear_tool_errors(
    http_app, caplog, user_headers, users, message
):
    app, _, requests, _ = http_app(users=users)
    async with app.router.lifespan_context(app):
        [(_, result)] = await call_over_http(app, [user_headers], sessions=1)
    assert result["result"]["isError"] is True
    assert message in result["result"]["content"][0]["text"]
    assert "/api/recipes" not in paths(requests)
    assert "carol" not in caplog.text


async def test_unhealthy_authentik_fails_startup_and_closes(load_server):
    server, clients, requests, _ = load_server(authentik_status=403)
    with pytest.raises(httpx.HTTPStatusError):
        async with server_lifespan(server["mcp"], "streamable-http"):
            pytest.fail("Startup must fail when Authentik rejects the service account")
    assert "/api/v3/core/users/me/" in paths(requests)
    assert "/api/app/about" not in paths(requests)
    assert clients.is_closed
