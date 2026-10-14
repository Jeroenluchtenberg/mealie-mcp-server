import json


def format_error_response(error_message: str) -> str:
    """Format error responses consistently as JSON strings."""
    error_response = {"success": False, "error": error_message}
    return json.dumps(error_response)


def format_api_params(params: dict) -> dict:
    """Drop None values from API parameters.

    Lists stay lists so httpx sends them as repeated keys (``tags=a&tags=b``),
    which is how Mealie's list query parameters are parsed. A comma-joined
    string would arrive as one unknown slug and Mealie would drop the filter.
    """
    return {k: v for k, v in params.items() if v is not None}
