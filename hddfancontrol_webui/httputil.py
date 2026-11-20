"""Small HTTP helpers shared by the core API (web.py) and the gateway (gateway.py)."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web


# ---------------------------
#   json_error
# ---------------------------
def json_error(status: int, code: str, problems: list[dict[str, Any]] | None = None) -> web.Response:
    """Build an error answer that the web UI translates by its code."""
    return web.json_response({"error": code, "problems": problems or []}, status=status)


# ---------------------------
#   read_json
# ---------------------------
async def read_json(request: web.Request) -> Any:
    """Read the JSON body, or raise a 400 answer.

    Also for a body nested too deep for the parser (RecursionError), and
    for an integer past Python's limit of digits (a plain ValueError, of
    which JSONDecodeError and UnicodeDecodeError are subclasses): a 500
    would log a traceback for each request, before any login.
    """
    try:
        return await request.json()
    except (ValueError, RecursionError) as err:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_json", "problems": []}), content_type="application/json"
        ) from err
