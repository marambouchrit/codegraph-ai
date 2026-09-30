"""Helpers for server URLs (Neo4j, Qdrant) shown in logs and error messages."""

from urllib.parse import urlsplit


def safe_uri(uri: str) -> str:
    """The URI without any "user:password@" part, safe to log or show."""
    try:
        parts = urlsplit(uri)
        host, port = parts.hostname or "", parts.port
    except ValueError:  # e.g. a port that is not a number
        return "<invalid URI>"
    if not parts.scheme:
        return "<invalid URI>"
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"
