import time
import random
import urllib.robotparser
from urllib.parse import urlparse

import requests

AGENT = "TimeSeriesBot/1.0 (+https://github.com/avivalbeg6/scraper)"
_robot_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


class RobotsDisallowed(Exception):
    """Raised when robots.txt disallows fetching the requested URL."""


def _robots(base_url: str) -> urllib.robotparser.RobotFileParser:
    if base_url not in _robot_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base_url + "/robots.txt")
        rp.read()
        _robot_cache[base_url] = rp
    return _robot_cache[base_url]


def fetch(url: str, **kwargs) -> requests.Response:
    """Fetch a URL politely: checks robots.txt, sleeps 2-5 s, retries on 429/5xx.

    Args:
        url: The URL to fetch.
        session: Optional requests.Session to reuse. Created fresh if not provided.
        **kwargs: Forwarded to session.get().

    Returns:
        The successful requests.Response.

    Raises:
        RobotsDisallowed: If robots.txt forbids the path for AGENT.
        requests.HTTPError: After all retry attempts are exhausted.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if not _robots(base).can_fetch(AGENT, url):
        raise RobotsDisallowed(url)

    time.sleep(random.uniform(2, 5))

    session = kwargs.pop("session", requests.Session())
    session.headers["User-Agent"] = AGENT

    delay = 2.0
    for attempt in range(5):
        resp = session.get(url, **kwargs)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            time.sleep(delay + random.random())
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
