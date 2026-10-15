"""Seed the local development Mealie with users, recipes, and cookbooks.

Run after `docker compose up -d`: `uv run python dev/seed.py`. Safe to rerun.
Writes dev/tokens.json (username -> Mealie API token) for the Authentik stub
and prints only usernames. Talks to the local stack only.
"""

import json
import os
import sys
from pathlib import Path

import httpx

BASE_URL = os.getenv("DEV_MEALIE_URL", "http://127.0.0.1:9925")
TOKENS_FILE = Path(__file__).with_name("tokens.json")
ADMIN = ("changeme@example.com", "MyPassword")
NEIGHBOUR = {
    "email": "neighbour@example.com",
    "username": "neighbour",
    "fullName": "Neighbour",
    "password": "NeighbourPassword1",
    "household": "Neighbours",
}

TAGS = ["Quick", "Soup"]
RECIPES = {
    "Stamppot": ["Quick"],
    "Pasta Pesto": ["Quick"],
    "Erwtensoep": ["Soup"],
    "Tomato Soup": ["Soup", "Quick"],
    "Slow Roast Lamb": [],
}
# Positions deliberately out of order.
COOKBOOKS = [
    ("Soups", 3, 'tags.name IN ["Soup"]'),
    ("Weeknight Dinners", 1, 'tags.name IN ["Quick"]'),
    ("Empty", 2, 'tags.name IN ["Nonexistent"]'),
]
NEIGHBOUR_COOKBOOKS = [("Neighbour Bakes", 1, "")]


def _check(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        sys.exit(f"{response.request.method} {response.request.url.path} -> {response.status_code}")
    return response


def _login(email: str, password: str) -> httpx.Client:
    response = _check(
        httpx.post(f"{BASE_URL}/api/auth/token", data={"username": email, "password": password})
    )
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
        timeout=30,
    )


def _api_token(client: httpx.Client) -> tuple[str, str]:
    """Create a fresh long-lived token for the logged-in user."""
    username = _check(client.get("/api/users/self")).json()["username"]
    token = _check(client.post("/api/users/api-tokens", json={"name": "mealie-mcp-dev"}))
    return username, token.json()["token"]


def _by_name(client: httpx.Client, path: str) -> dict:
    items = _check(client.get(path, params={"perPage": -1})).json()["items"]
    return {item["name"]: item for item in items}


def _ensure_household_and_user(admin: httpx.Client) -> None:
    group = _check(admin.get("/api/users/self")).json()
    if NEIGHBOUR["household"] not in _by_name(admin, "/api/admin/households"):
        _check(
            admin.post(
                "/api/admin/households",
                json={"name": NEIGHBOUR["household"], "groupId": group["groupId"]},
            )
        )
    users = _check(admin.get("/api/admin/users", params={"perPage": -1})).json()["items"]
    if not any(user["username"] == NEIGHBOUR["username"] for user in users):
        _check(admin.post("/api/admin/users", json={**NEIGHBOUR, "group": group["group"]}))


def _ensure_recipes(client: httpx.Client) -> None:
    tags = _by_name(client, "/api/organizers/tags")
    for name in TAGS:
        if name not in tags:
            tags[name] = _check(client.post("/api/organizers/tags", json={"name": name})).json()
    existing = _by_name(client, "/api/recipes")
    for name, tag_names in RECIPES.items():
        if name in existing:
            continue
        slug = _check(client.post("/api/recipes", json={"name": name})).json()
        recipe = _check(client.get(f"/api/recipes/{slug}")).json()
        recipe["tags"] = [tags[tag] for tag in tag_names]
        recipe["totalTime"] = "30 min"
        _check(client.put(f"/api/recipes/{slug}", json=recipe))


def _ensure_cookbooks(client: httpx.Client, cookbooks: list) -> None:
    existing = _by_name(client, "/api/households/cookbooks")
    for name, position, query_filter in cookbooks:
        if name in existing:
            continue
        _check(
            client.post(
                "/api/households/cookbooks",
                json={
                    "name": name,
                    "description": f"{name} recipes",
                    "position": position,
                    "public": False,
                    "queryFilterString": query_filter,
                },
            )
        )


def main() -> None:
    admin = _login(*ADMIN)
    _ensure_household_and_user(admin)
    neighbour = _login(NEIGHBOUR["email"], NEIGHBOUR["password"])
    _ensure_recipes(admin)
    _ensure_cookbooks(admin, COOKBOOKS)
    _ensure_cookbooks(neighbour, NEIGHBOUR_COOKBOOKS)

    tokens = dict(_api_token(client) for client in (admin, neighbour))
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    TOKENS_FILE.chmod(0o644)  # read by the stub container's user
    print(f"Seeded {BASE_URL}; X-User values: {', '.join(tokens)}")


if __name__ == "__main__":
    main()
