import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from identity import AuthentikTokenStore, RequestCredentials
from mealie import MealieFetcher
from prompts import register_prompts
from tools import register_ai_import_tools, register_all_tools

logger = logging.getLogger("mealie-mcp")
Transport = Literal["sse", "streamable-http"]


@dataclass(frozen=True)
class ServerConfig:
    base_url: str = field(repr=False)
    authentik_url: str = field(repr=False)
    authentik_token: str = field(repr=False)
    user_header: str = "X-User"
    token_attribute: str = "mealie_token"
    token_cache_ttl: float = 120.0
    transport: Transport = "streamable-http"
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    enable_ai_import: bool = False

    @classmethod
    def from_environment(cls) -> "ServerConfig":
        load_dotenv()
        ai_import = os.getenv("MEALIE_ENABLE_AI_IMPORT", "false").strip().lower()
        if ai_import not in ("true", "false"):
            raise ValueError("MEALIE_ENABLE_AI_IMPORT must be true or false")
        if os.getenv("MEALIE_API_KEY") is not None:
            raise ValueError(
                "MEALIE_API_KEY is no longer supported; each user's Mealie token "
                "is read from Authentik. Remove it and set AUTHENTIK_URL and "
                "AUTHENTIK_TOKEN."
            )
        base_url = os.getenv("MEALIE_BASE_URL")
        authentik_url = os.getenv("AUTHENTIK_URL")
        authentik_token = os.getenv("AUTHENTIK_TOKEN")
        if not base_url or not authentik_url or not authentik_token:
            raise ValueError(
                "MEALIE_BASE_URL, AUTHENTIK_URL, and AUTHENTIK_TOKEN must be set "
                "in environment variables."
            )
        user_header = os.getenv("MCP_USER_HEADER", "X-User").strip()
        if not user_header:
            raise ValueError("MCP_USER_HEADER cannot be empty")
        token_attribute = os.getenv("AUTHENTIK_TOKEN_ATTRIBUTE", "mealie_token").strip()
        if not token_attribute:
            raise ValueError("AUTHENTIK_TOKEN_ATTRIBUTE cannot be empty")
        try:
            token_cache_ttl = float(os.getenv("MEALIE_TOKEN_CACHE_TTL", "120"))
        except ValueError:
            raise ValueError("MEALIE_TOKEN_CACHE_TTL must be a number of seconds") from None
        if not 0 <= token_cache_ttl <= 3600:
            raise ValueError("MEALIE_TOKEN_CACHE_TTL must be between 0 and 3600 seconds")
        transport = os.getenv("MCP_TRANSPORT", "streamable-http")
        if transport not in ("sse", "streamable-http"):
            raise ValueError(
                "MCP_TRANSPORT must be sse or streamable-http; stdio has no "
                "per-user identity"
            )
        try:
            port = int(os.getenv("MCP_PORT", "8765"))
        except ValueError:
            raise ValueError("MCP_PORT must be an integer between 1 and 65535") from None
        if not 1 <= port <= 65535:
            raise ValueError("MCP_PORT must be an integer between 1 and 65535")
        level = logging.getLevelNamesMapping().get(
            os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO
        )
        log_level = logging.getLevelName(level)
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            log_level = "INFO"
        return cls(
            base_url=base_url,
            authentik_url=authentik_url,
            authentik_token=authentik_token,
            user_header=user_header,
            token_attribute=token_attribute,
            token_cache_ttl=token_cache_ttl,
            transport=transport,
            host=os.getenv("MCP_HOST", "127.0.0.1"),
            port=port,
            log_level=log_level,
            enable_ai_import=ai_import == "true",
        )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    logger.setLevel(level)


class _ClientProvider:
    def __init__(self, config: ServerConfig | None):
        self.config = config
        self._client: MealieFetcher | None = None
        self._tokens: AuthentikTokenStore | None = None
        self._users = 0

    def configuration(self) -> ServerConfig:
        if self.config is None:
            self.config = ServerConfig.from_environment()
        return self.config

    def __getattr__(self, name: str) -> Any:
        if self._client is None:
            raise RuntimeError("Mealie tools require an active server lifespan")
        return getattr(self._client, name)

    @contextmanager
    def acquire(self) -> Iterator[MealieFetcher]:
        if self._client is None:
            config = self.configuration()
            _configure_logging(config.log_level)
            tokens = AuthentikTokenStore(
                config.authentik_url,
                config.authentik_token,
                attribute=config.token_attribute,
                ttl=config.token_cache_ttl,
            )
            try:
                self._client = MealieFetcher(
                    config.base_url, RequestCredentials(tokens, config.user_header)
                )
            except BaseException:
                tokens.close()
                raise
            self._tokens = tokens
        self._users += 1
        try:
            yield self._client
        finally:
            self._users -= 1
            if self._users == 0:
                client, self._client = self._client, None
                tokens, self._tokens = self._tokens, None
                try:
                    client.close()
                finally:
                    tokens.close()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[MealieFetcher]:
        with self.acquire() as client:
            yield client


class _MealieServer(FastMCP):
    def __init__(self, config: ServerConfig | None):
        self.client_provider = _ClientProvider(config)
        self._ai_import_registered = False

        @asynccontextmanager
        async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, MealieFetcher]]:
            self._configure_optional_tools()
            async with self.client_provider.lifespan() as client:
                yield {"mealie": client}

        super().__init__(
            "mealie",
            host=config.host if config else "127.0.0.1",
            port=config.port if config else 8765,
            log_level=config.log_level if config else "INFO",
            lifespan=lifespan,
            # Every call carries its own gateway identity and nothing is
            # session-bound, so sessions only go stale across restarts.
            stateless_http=True,
        )

    def _configure_optional_tools(self) -> None:
        config = self.client_provider.configuration()
        if config.enable_ai_import and not self._ai_import_registered:
            register_ai_import_tools(self, self.client_provider)
            self._ai_import_registered = True

    def run(
        self, transport: Transport = "streamable-http", mount_path: str | None = None
    ) -> None:
        config = self.client_provider.configuration()
        self._configure_optional_tools()
        self.settings.host = config.host
        self.settings.port = config.port
        self.settings.log_level = config.log_level
        _configure_logging(config.log_level)
        # Uvicorn consumes ASGI startup exceptions; fail before entering it so
        # unsuccessful health checks also produce a failing console exit status.
        with self.client_provider.acquire():
            super().run(transport=transport, mount_path=mount_path)

    def _application_lifespan(self, app):
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            self._configure_optional_tools()
            # SDK lifespans are per MCP session. HTTP also needs an outer owner
            # to check health at startup and keep the client alive between sessions.
            async with self.client_provider.lifespan():
                async with original_lifespan(application) as state:
                    yield state

        app.router.lifespan_context = lifespan
        return app

    def streamable_http_app(self):
        return self._application_lifespan(super().streamable_http_app())

    def sse_app(self, mount_path: str | None = None):
        return self._application_lifespan(super().sse_app(mount_path))


def create_server(config: ServerConfig | None = None) -> FastMCP:
    """Build a discoverable server without reading configuration or opening HTTP."""
    server = _MealieServer(config)
    register_prompts(server)
    register_all_tools(server, server.client_provider)
    if config is not None:
        server._configure_optional_tools()
    return server


# The SDK's `mcp dev` command discovers this instance without needing credentials.
mcp = create_server()


def main() -> None:
    try:
        config = ServerConfig.from_environment()
        _configure_logging(config.log_level)
        server = create_server(config)
        logger.info({"operation": "Starting Mealie MCP server"})
        server.run(transport=config.transport)
    except Exception as error:
        logger.critical(
            {"operation": "Running Mealie MCP server", "error_type": type(error).__name__}
        )
        raise


if __name__ == "__main__":
    main()
