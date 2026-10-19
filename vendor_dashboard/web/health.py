"""Public liveness only: no credentials, database queries or vendor data."""

from starlette.requests import Request
from starlette.responses import JSONResponse


async def health(_request: Request) -> JSONResponse:
    """Report a responsive process, without promising dependency readiness."""
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
