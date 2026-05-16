"""Backward-compatibility shim. The canonical implementation lives at src/scrapers/http_client.py."""

from src.scrapers.http_client import AGENT, _robots, fetch  # noqa: F401

# Historical alias: the module previously raised this custom type.
# Code that catches RobotsDisallowed will still catch RuntimeError.
RobotsDisallowed = RuntimeError
