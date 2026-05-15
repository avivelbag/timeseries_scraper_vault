import sys
import os
import urllib.robotparser
from unittest.mock import MagicMock, patch, call

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.http_client import fetch, RobotsDisallowed, AGENT, _robot_cache


def _make_response(status_code: int) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    if status_code < 400:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


@pytest.fixture(autouse=True)
def clear_robot_cache():
    """Isolate each test from cached robots.txt state."""
    _robot_cache.clear()
    yield
    _robot_cache.clear()


@pytest.fixture()
def robots_allow():
    """Patch _robots to always allow any URL."""
    rp = MagicMock(spec=urllib.robotparser.RobotFileParser)
    rp.can_fetch.return_value = True
    with patch("src.http_client._robots", return_value=rp):
        yield rp


@pytest.fixture()
def no_sleep():
    with patch("src.http_client.time.sleep") as mock_sleep:
        yield mock_sleep


class TestRobotsBlocking:
    def test_raises_when_disallowed(self, no_sleep):
        rp = MagicMock(spec=urllib.robotparser.RobotFileParser)
        rp.can_fetch.return_value = False
        with patch("src.http_client._robots", return_value=rp):
            with pytest.raises(RobotsDisallowed) as exc_info:
                fetch("https://example.com/private/data")
        assert "https://example.com/private/data" in str(exc_info.value)

    def test_checks_correct_agent_and_url(self, no_sleep):
        rp = MagicMock(spec=urllib.robotparser.RobotFileParser)
        rp.can_fetch.return_value = False
        with patch("src.http_client._robots", return_value=rp):
            with pytest.raises(RobotsDisallowed):
                fetch("https://example.com/blocked")
        rp.can_fetch.assert_called_once_with(AGENT, "https://example.com/blocked")

    def test_no_http_call_when_blocked(self, no_sleep):
        rp = MagicMock(spec=urllib.robotparser.RobotFileParser)
        rp.can_fetch.return_value = False
        session = MagicMock(spec=requests.Session)
        with patch("src.http_client._robots", return_value=rp):
            with pytest.raises(RobotsDisallowed):
                fetch("https://example.com/blocked", session=session)
        session.get.assert_not_called()


class TestSuccessfulFetch:
    def test_returns_response_on_200(self, robots_allow, no_sleep):
        resp_200 = _make_response(200)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.return_value = resp_200

        result = fetch("https://example.com/data", session=session)

        assert result is resp_200
        session.get.assert_called_once_with("https://example.com/data")

    def test_sets_user_agent_header(self, robots_allow, no_sleep):
        resp_200 = _make_response(200)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.return_value = resp_200

        fetch("https://example.com/data", session=session)

        assert session.headers["User-Agent"] == AGENT

    def test_sleeps_before_request(self, robots_allow):
        resp_200 = _make_response(200)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.return_value = resp_200

        with patch("src.http_client.time.sleep") as mock_sleep:
            with patch("src.http_client.random.uniform", return_value=3.0):
                fetch("https://example.com/data", session=session)

        mock_sleep.assert_any_call(3.0)

    def test_kwargs_forwarded_to_get(self, robots_allow, no_sleep):
        resp_200 = _make_response(200)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.return_value = resp_200

        fetch("https://example.com/data", session=session, timeout=10)

        session.get.assert_called_once_with("https://example.com/data", timeout=10)


class TestRetryBehaviour:
    def test_429_triggers_retry_and_succeeds(self, robots_allow, no_sleep):
        resp_429 = _make_response(429)
        resp_200 = _make_response(200)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = [resp_429, resp_200]

        result = fetch("https://example.com/data", session=session)

        assert result is resp_200
        assert session.get.call_count == 2

    def test_500_triggers_retry(self, robots_allow, no_sleep):
        resp_500 = _make_response(500)
        resp_200 = _make_response(200)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = [resp_500, resp_200]

        result = fetch("https://example.com/data", session=session)

        assert result is resp_200

    def test_backoff_sleep_called_on_retry(self, robots_allow):
        resp_429 = _make_response(429)
        resp_200 = _make_response(200)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = [resp_429, resp_200]

        with patch("src.http_client.time.sleep") as mock_sleep:
            with patch("src.http_client.random.uniform", return_value=2.5):
                with patch("src.http_client.random.random", return_value=0.5):
                    fetch("https://example.com/data", session=session)

        # First sleep is the pre-request polite delay; second is the backoff
        sleep_calls = mock_sleep.call_args_list
        assert len(sleep_calls) == 2
        assert sleep_calls[1] == call(2.0 + 0.5)  # base delay + jitter

    def test_all_retries_exhausted_raises(self, robots_allow, no_sleep):
        """Five consecutive 429s should exhaust retries and raise HTTPError."""
        resp_429 = _make_response(429)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.return_value = resp_429

        with pytest.raises(requests.HTTPError):
            fetch("https://example.com/data", session=session)

        assert session.get.call_count == 5

    def test_5xx_codes_all_trigger_retry(self, robots_allow, no_sleep):
        for code in (500, 502, 503, 504):
            _robot_cache.clear()
            resp_err = _make_response(code)
            resp_200 = _make_response(200)
            session = MagicMock(spec=requests.Session)
            session.headers = {}
            session.get.side_effect = [resp_err, resp_200]

            result = fetch("https://example.com/data", session=session)
            assert result is resp_200, f"Expected success after retry for {code}"

    def test_non_retryable_4xx_raises_immediately(self, robots_allow, no_sleep):
        """A 404 should not be retried — it should raise on the first attempt."""
        resp_404 = _make_response(404)
        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.get.return_value = resp_404

        with pytest.raises(requests.HTTPError):
            fetch("https://example.com/missing", session=session)

        assert session.get.call_count == 1
