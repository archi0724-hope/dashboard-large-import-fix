"""Compose lightweight HTTP routes without opening storage."""

from starlette.routing import Route

from vendor_dashboard.web.health import health


def health_routes() -> list[Route]:
    """Keep HTTP imports separate from the UI script and its actions."""
    return [Route("/api/health", health, methods=["GET"])]
