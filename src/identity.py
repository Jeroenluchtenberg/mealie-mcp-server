"""Per-request Mealie credentials for the gateway-authenticated user.

The MCP gateway validates the user's OAuth token and forwards a verified
username in a request header. Each user's Mealie API token is stored as an
attribute on their Authentik account and read with a read-only service account.
Usernames and tokens are never logged.
"""

import logging
import threading
import time
from collections.abc import Callable

import httpx
from mcp.server.lowlevel.server import request_ctx

logger = logging.getLogger("mealie-mcp")

NO_USER_MESSAGE = (
    "No authenticated user on this request; connect through the MCP gateway."
)
NO_TOKEN_MESSAGE = (
    "No Mealie token linked to your account; ask the admin to set it up."
)
LOOKUP_FAILED_MESSAGE = (
    "Could not look up your Mealie token; try again later or contact the admin."
)


class CredentialError(Exception):
    """A credential problem whose message is safe to show to the user."""


def current_user(header: str) -> str | None:
    """Return the gateway-supplied username for the MCP request being handled."""
    try:
        context = request_ctx.get()
    except LookupError:
        return None
    headers = getattr(context.request, "headers", None)
    if headers is None:
        return None
    value = (headers.get(header) or "").strip()
    return value or None


class AuthentikTokenStore:
    """Read Mealie API tokens from Authentik user attributes, with a short cache.

    Only successful lookups are cached, so a token the admin just linked is used
    on the next request. A short TTL makes deactivating a user or removing the
    attribute take effect within minutes.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        attribute: str = "mealie_token",
        ttl: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not base_url:
            raise ValueError("Authentik URL cannot be empty")
        if not api_token:
            raise ValueError("Authentik token cannot be empty")
        self._attribute = attribute
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=10.0,
        )
        try:
            response = self._client.get("/api/v3/core/users/me/")
            response.raise_for_status()
        except BaseException as error:
            self._client.close()
            self._log_failure("Checking Authentik access", error)
            if isinstance(error, httpx.HTTPStatusError):
                raise httpx.HTTPStatusError(
                    "Authentik API access check failed",
                    request=error.request,
                    response=error.response,
                ) from None
            if isinstance(error, httpx.TimeoutException):
                raise TimeoutError("Authentik API access check timed out") from None
            if isinstance(error, httpx.RequestError):
                raise ConnectionError("Could not connect to the Authentik API") from None
            raise
        logger.info({"operation": "Connected to Authentik API"})

    @staticmethod
    def _log_failure(operation: str, error: BaseException) -> None:
        metadata = {"operation": operation, "error_type": type(error).__name__}
        if isinstance(error, httpx.HTTPStatusError):
            metadata["status_code"] = error.response.status_code
        logger.error(metadata)

    def token_for(self, username: str) -> str:
        """Return the Mealie token linked to an active Authentik user."""
        with self._lock:
            cached = self._cache.get(username)
            if cached and cached[0] > self._clock():
                return cached[1]
        token = self._lookup(username)
        with self._lock:
            self._cache[username] = (self._clock() + self._ttl, token)
        return token

    def forget(self, username: str) -> None:
        """Drop a cached token, e.g. after Mealie rejected it."""
        with self._lock:
            self._cache.pop(username, None)

    def _lookup(self, username: str) -> str:
        try:
            response = self._client.get(
                "/api/v3/core/users/",
                params={"username": username, "is_active": "true"},
            )
            response.raise_for_status()
            users = response.json().get("results") or []
        except (httpx.HTTPError, ValueError, AttributeError) as error:
            self._log_failure("Looking up Mealie token", error)
            raise CredentialError(LOOKUP_FAILED_MESSAGE) from None
        matches = [
            user
            for user in users
            if isinstance(user, dict)
            and user.get("username") == username
            and user.get("is_active", True)
        ]
        if len(matches) != 1:
            raise CredentialError(NO_TOKEN_MESSAGE)
        token = (matches[0].get("attributes") or {}).get(self._attribute)
        if not isinstance(token, str) or not token.strip():
            raise CredentialError(NO_TOKEN_MESSAGE)
        return token.strip()

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()


class RequestCredentials:
    """Token provider that resolves the Mealie token for the current request."""

    def __init__(self, store: AuthentikTokenStore, header: str = "X-User"):
        self._store = store
        self._header = header

    def __call__(self) -> str:
        username = current_user(self._header)
        if username is None:
            raise CredentialError(NO_USER_MESSAGE)
        return self._store.token_for(username)

    def invalidate(self) -> None:
        """Forget the current user's cached token so the next call re-reads it."""
        username = current_user(self._header)
        if username is not None:
            self._store.forget(username)
