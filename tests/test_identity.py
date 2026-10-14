"""Per-user Mealie token lookup through Authentik, without network access."""

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx

from identity import (
    LOOKUP_FAILED_MESSAGE,
    NO_TOKEN_MESSAGE,
    NO_USER_MESSAGE,
    AuthentikTokenStore,
    CredentialError,
    RequestCredentials,
    current_user,
)
from mealie.client import MealieApiError
from tools.errors import tool_error_boundary

RealClient = httpx.Client


@contextmanager
def as_request(headers):
    token = request_ctx.set(SimpleNamespace(request=SimpleNamespace(headers=headers)))
    try:
        yield
    finally:
        request_ctx.reset(token)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def authentik(monkeypatch, request):
    """Build a token store against a fake Authentik that serves ``state``."""
    state = {
        "me_status": 200,
        "users_status": 200,
        "attributes": {"alice": {"mealie_token": "alice-token"}},
        "failure": None,
    }
    requests = []
    clients = []

    def respond(api_request):
        requests.append(api_request)
        if api_request.url.path == "/api/v3/core/users/me/":
            return httpx.Response(state["me_status"], json={})
        if state["failure"]:
            raise state["failure"]
        if state["users_status"] != 200:
            return httpx.Response(state["users_status"], json={"detail": "private-detail"})
        username = api_request.url.params["username"]
        results = [
            {"username": name, "is_active": True, "attributes": attributes}
            for name, attributes in state["attributes"].items()
            if name == username
        ]
        return httpx.Response(200, json={"results": results})

    def build(**kwargs):
        client = RealClient(**kwargs, transport=httpx.MockTransport(respond))
        clients.append(client)
        request.addfinalizer(client.close)
        return client

    monkeypatch.setattr("identity.httpx.Client", Mock(side_effect=build))
    clock = Clock()

    def make(**kwargs):
        return AuthentikTokenStore(
            "http://authentik.invalid", "service-token", clock=clock, **kwargs
        )

    return SimpleNamespace(
        make=make, state=state, requests=requests, clients=clients, clock=clock
    )


def lookups(authentik):
    return [r for r in authentik.requests if r.url.path == "/api/v3/core/users/"]


def test_startup_checks_service_account(authentik):
    store = authentik.make()
    first = authentik.requests[0]
    assert first.url.path == "/api/v3/core/users/me/"
    assert first.headers["authorization"] == "Bearer service-token"
    store.close()
    assert authentik.clients[0].is_closed


@pytest.mark.parametrize("status", [401, 403, 500])
def test_rejected_service_account_fails_startup_and_closes(authentik, status):
    authentik.state["me_status"] = status
    with pytest.raises(httpx.HTTPStatusError, match="Authentik API access check failed"):
        authentik.make()
    assert authentik.clients[0].is_closed


@pytest.mark.parametrize(
    "base_url,token", [("", "service-token"), ("http://authentik.invalid", "")]
)
def test_store_requires_url_and_token(base_url, token):
    with pytest.raises(ValueError):
        AuthentikTokenStore(base_url, token)


def test_tokens_are_cached_until_the_ttl_expires(authentik):
    store = authentik.make(ttl=60)
    assert store.token_for("alice") == "alice-token"
    authentik.state["attributes"]["alice"]["mealie_token"] = "rotated-token"
    authentik.clock.now += 59
    assert store.token_for("alice") == "alice-token"
    assert len(lookups(authentik)) == 1
    authentik.clock.now += 2
    assert store.token_for("alice") == "rotated-token"
    assert len(lookups(authentik)) == 2


def test_removed_users_lose_access_after_the_ttl(authentik):
    store = authentik.make(ttl=60)
    store.token_for("alice")
    del authentik.state["attributes"]["alice"]
    authentik.clock.now += 61
    with pytest.raises(CredentialError, match=NO_TOKEN_MESSAGE):
        store.token_for("alice")


def test_missing_tokens_are_not_cached(authentik):
    store = authentik.make(ttl=60)
    authentik.state["attributes"] = {"alice": {}}
    with pytest.raises(CredentialError, match=NO_TOKEN_MESSAGE):
        store.token_for("alice")
    authentik.state["attributes"] = {"alice": {"mealie_token": "linked-token"}}
    assert store.token_for("alice") == "linked-token"


@pytest.mark.parametrize("value", [None, "", "   ", 42, ["token"]])
def test_unusable_attribute_values_are_rejected(authentik, value):
    authentik.state["attributes"] = {"alice": {"mealie_token": value}}
    with pytest.raises(CredentialError, match=NO_TOKEN_MESSAGE):
        authentik.make().token_for("alice")


def test_custom_attribute_name(authentik):
    authentik.state["attributes"] = {"alice": {"recipes_key": " custom-token "}}
    assert authentik.make(attribute="recipes_key").token_for("alice") == "custom-token"


def test_forget_forces_a_fresh_lookup(authentik):
    store = authentik.make(ttl=60)
    store.token_for("alice")
    store.forget("alice")
    store.forget("never-cached")
    store.token_for("alice")
    assert len(lookups(authentik)) == 2


@pytest.mark.parametrize(
    "failure,status",
    [
        (None, 500),
        (None, 403),
        (httpx.ConnectError("private-transport"), 200),
        (httpx.ReadTimeout("private-transport"), 200),
    ],
)
def test_lookup_failures_are_reported_without_details(authentik, caplog, failure, status):
    store = authentik.make()
    authentik.state["failure"] = failure
    authentik.state["users_status"] = status
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CredentialError) as caught:
            store.token_for("private-user")
    assert str(caught.value) == LOOKUP_FAILED_MESSAGE
    for private in ("private-user", "private-transport", "private-detail", "service-token"):
        assert private not in caplog.text


def test_current_user_reads_the_configured_header():
    assert current_user("X-User") is None
    with as_request({"X-Forwarded-User": " alice "}):
        assert current_user("X-Forwarded-User") == "alice"
        assert current_user("X-User") is None
    with as_request({"X-User": ""}):
        assert current_user("X-User") is None
    token = request_ctx.set(SimpleNamespace(request=None))
    try:
        assert current_user("X-User") is None
    finally:
        request_ctx.reset(token)


def test_request_credentials_resolve_and_invalidate(authentik):
    store = authentik.make(ttl=60)
    credentials = RequestCredentials(store)
    with pytest.raises(CredentialError, match=NO_USER_MESSAGE):
        credentials()
    credentials.invalidate()  # no request: nothing to forget
    with as_request({"X-User": "alice"}):
        assert credentials() == "alice-token"
        credentials.invalidate()
        assert credentials() == "alice-token"
    assert len(lookups(authentik)) == 2


@pytest.mark.parametrize(
    "error,expected",
    [
        (CredentialError(NO_TOKEN_MESSAGE), NO_TOKEN_MESSAGE),
        (MealieApiError(401, "private"), "ask the admin to renew it"),
        (MealieApiError(403, "private"), "ask the admin for the required permission"),
        (MealieApiError(500, "private"), "Mealie API request failed (HTTP 500)"),
    ],
)
def test_tool_boundary_explains_credential_failures(error, expected):
    with pytest.raises(ToolError) as caught:
        with tool_error_boundary("Error fetching recipes"):
            raise error
    assert str(caught.value).startswith("Error fetching recipes: ")
    assert expected in str(caught.value)
    assert "private" not in str(caught.value)
