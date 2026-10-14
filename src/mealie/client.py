import json
import logging
from collections.abc import Callable
from http.cookiejar import CookieJar
from typing import Any, Dict

import httpx

logger = logging.getLogger("mealie-mcp")


class MealieApiError(Exception):
    """Custom exception for Mealie API errors with status code and response details."""

    def __init__(self, status_code: int, message: str, response_text: str | None = None):
        self.status_code = status_code
        self.message = message
        self.response_text = response_text
        super().__init__(f"{message} (Status Code: {status_code})")


class _NoCookies(CookieJar):
    """Cookie jar that stores nothing, so no session state leaks between users."""

    def set_cookie(self, cookie) -> None:
        pass

    def extract_cookies(self, response, request) -> None:
        pass


class MealieClient:
    """Shared Mealie connection that authenticates each request separately.

    ``credentials`` returns the API token for the user behind the current
    request. If it also has an ``invalidate()`` method, that is called when
    Mealie rejects the token, so a cached token is re-read on the next request.
    """

    def __init__(self, base_url: str, credentials: Callable[[], str]):
        if not base_url:
            raise ValueError("Base URL cannot be empty")
        if not callable(credentials):
            raise ValueError("Credentials provider must be callable")
        self._credentials = credentials

        # Third-party debug/info logs include request URLs and connection details.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logger.debug({"operation": "Initializing Mealie client"})
        self._client = httpx.Client(
            base_url=base_url,
            cookies=_NoCookies(),
            timeout=30.0,
        )
        try:
            # The about endpoint is public, so the health check needs no user token.
            response = self._client.get("/api/app/about")
            response.raise_for_status()
        except BaseException as error:
            self._client.close()
            self._log_failure("Checking Mealie health", error)
            if isinstance(error, httpx.HTTPStatusError):
                raise httpx.HTTPStatusError(
                    "Mealie API health check failed",
                    request=error.request,
                    response=error.response,
                ) from None
            if isinstance(error, httpx.TimeoutException):
                raise TimeoutError("Mealie API health check timed out") from None
            if isinstance(error, httpx.RequestError):
                raise ConnectionError("Could not connect to the Mealie API") from None
            raise
        logger.info({"operation": "Connected to Mealie API"})

    @staticmethod
    def _log_failure(operation: str, error: BaseException) -> None:
        metadata = {"operation": operation, "error_type": type(error).__name__}
        if isinstance(error, httpx.HTTPStatusError):
            metadata["status_code"] = error.response.status_code
        logger.error(metadata)

    def _handle_request(self, method: str, url: str, **kwargs) -> Dict[str, Any] | str:
        """Common request handler with error handling for all API calls.

        Supports:
        - JSON requests via json= parameter
        - Multipart uploads via files= parameter
        - Form data via data= parameter
        """
        # Leave multipart boundaries to httpx and do not mutate caller headers.
        headers = httpx.Headers(kwargs.get("headers"))
        headers["Authorization"] = f"Bearer {self._credentials()}"
        if "json" in kwargs and "files" not in kwargs:
            headers["Content-Type"] = "application/json"
        kwargs["headers"] = headers
        try:
            logger.debug({"operation": "Making Mealie API request"})

            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()  # Raise an exception for 4XX/5XX responses

            logger.debug(
                {"operation": "Mealie API request succeeded", "status_code": response.status_code}
            )

            # Handle empty responses (common for DELETE operations)
            if response.status_code == 204 or not response.content:
                return {"success": True, "message": "Operation completed successfully"}

            try:
                response_data = response.json()
                # Normalize JSON null to success dict (common for DELETE operations)
                if response_data is None:
                    return {"success": True, "message": "Operation completed successfully"}

                return response_data
            except json.JSONDecodeError:
                if not response.text.strip():
                    return {"success": True, "message": "Operation completed successfully"}
                return response.text

        except httpx.HTTPStatusError as error:
            self._log_failure("Requesting Mealie API", error)
            if error.response.status_code == 401:
                invalidate = getattr(self._credentials, "invalidate", None)
                if callable(invalidate):
                    invalidate()
            raise MealieApiError(
                error.response.status_code,
                "Mealie API request failed",
                error.response.text,
            ) from None
        except httpx.TimeoutException as error:
            self._log_failure("Requesting Mealie API", error)
            raise TimeoutError("Mealie API request timed out") from None
        except httpx.RequestError as error:
            self._log_failure("Requesting Mealie API", error)
            raise ConnectionError("Could not communicate with the Mealie API") from None
        except Exception as error:
            self._log_failure("Requesting Mealie API", error)
            raise

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()
