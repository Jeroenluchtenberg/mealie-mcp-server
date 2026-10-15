"""Minimal Authentik stand-in for the local development stack.

Answers only the two Authentik API calls the server makes: the startup access
check and the per-user lookup of the Mealie token attribute. Tokens come from
a JSON file mapping username to Mealie token, written by dev/seed.py and
re-read on every request. Never use this outside local development.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

API_TOKEN = os.environ["AUTHENTIK_TOKEN"]
TOKENS_FILE = Path(os.getenv("TOKENS_FILE", "/data/tokens.json"))
ATTRIBUTE = os.getenv("AUTHENTIK_TOKEN_ATTRIBUTE", "mealie_token")
PORT = int(os.getenv("PORT", "9001"))


def _tokens() -> dict:
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.headers.get("Authorization") != f"Bearer {API_TOKEN}":
            return self._send(403, {"detail": "invalid token"})
        url = urlsplit(self.path)
        if url.path == "/api/v3/core/users/me/":
            return self._send(200, {"user": {"username": "mealie-mcp-dev"}})
        if url.path == "/api/v3/core/users/":
            username = (parse_qs(url.query).get("username") or [""])[0]
            token = _tokens().get(username)
            results = []
            if token:
                results.append(
                    {"username": username, "is_active": True, "attributes": {ATTRIBUTE: token}}
                )
            return self._send(200, {"results": results})
        return self._send(404, {"detail": "not found"})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Request lines carry usernames; keep the stub quiet.
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
