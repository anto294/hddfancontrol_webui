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
    """Read the JSON body, or raise a 400 answer (also for one nested too deep for the parser)."""
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as err:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_json", "problems": []}), content_type="application/json"
        ) from err
