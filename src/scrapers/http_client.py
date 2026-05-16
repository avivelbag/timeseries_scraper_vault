"""Shared HTTP client for all scrapers.

Enforces robots.txt compliance, polite rate-limiting, and exponential backoff
uniformly so individual scrapers need only call fetch().
"""

import random
import time
import urllib.robotparser
from functools import lru_cache
from urllib.parse import urlparse

import requests

AGENT = "TimeSeriesBot/1.0 (+https://github.com/avivalbeg/circle-jerk2)"


@lru_cache(maxsize=256)
def _robots(base_url: str) -> urllib.robotparser.RobotFileParser:
    """Return a cached RobotFileParser for the given base URL.

    Args:
        base_url: Scheme + netloc (e.g. "https://example.com").

    Returns:
        Parsed RobotFileParser for the domain's robots.txt.
    """
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(base_url + "/robots.txt")
    rp.read()
    return rp


def fetch(
    url: str,
    *,
    session: requests.Session | None = None,
    min_delay: float = 2.0,
    max_delay: float = 5.0,
    **kwargs: object,
) -> requests.Response:
    """Fetch a URL with robots.txt compliance, polite delay, and exponential backoff.

    Checks robots.txt once per domain per process (result is cached). Sleeps a
    random duration in [min_delay, max_delay] before every request. Retries on
    429 or 5xx with exponential backoff (base 2, cap 120 s) up to 5 attempts,
    then raises. Sets User-Agent to AGENT on every request.

    Args:
        url: The URL to fetch.
        session: Optional requests.Session to reuse. Created fresh if None.
        min_delay: Lower bound of the pre-request polite sleep (seconds).
        max_delay: Upper bound of the pre-request polite sleep (seconds).
        **kwargs: Forwarded verbatim to session.get().

    Returns:
        The successful requests.Response.

    Raises:
        RuntimeError: If robots.txt forbids the path for AGENT.
        requests.HTTPError: After all 5 retry attempts are exhausted.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if not _robots(base).can_fetch(AGENT, url):
        raise RuntimeError(f"robots.txt disallows: {url}")

    time.sleep(random.uniform(min_delay, max_delay))

    if session is None:
        session = requests.Session()
    session.headers["User-Agent"] = AGENT

    delay = 2.0
    for attempt in range(5):
        resp = session.get(url, **kwargs)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            time.sleep(min(delay + random.random(), 120))
            delay = min(delay * 2, 120)
            continue
        resp.raise_for_status()
        return resp
    return resp
