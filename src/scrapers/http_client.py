import random
import time
import urllib.robotparser
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import requests

AGENT = "TimeSeriesBot/1.0 (+https://github.com/avivalbeg/circle-jerk2)"


@lru_cache(maxsize=256)
def _robots(base_url: str) -> urllib.robotparser.RobotFileParser:
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
    **kwargs: Any,
) -> requests.Response:
    """Fetch url enforcing robots.txt, polite delay, and exponential backoff.

    Raises RuntimeError if robots.txt disallows the path. Sleeps [min_delay,
    max_delay] before each request. Retries 429/5xx up to 5 times (base-2
    backoff, cap 120 s) then raises HTTPError.
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
