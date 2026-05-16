"""Tests for src/scrapers/noaa_sea_level.py.

All tests use a static HTML fixture or inline HTML strings — no live network
calls are made.
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.noaa_sea_level import (
    _DEFAULT_STATIONS,
    _MIN_STATION_SLEEP,
    _extract_station_name,
    _station_url,
    parse_html,
    scrape,
    scrape_station,
)
from protos.noaa_sea_level_pb2 import SeaLevelRecord
from bs4 import BeautifulSoup

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "noaa_sea_level_8518750.html"
)
STATION_ID = "8518750"
STATION_NAME = "The Battery, New York"
STATION_URL = _station_url(STATION_ID)


@pytest.fixture
def fixture_html() -> str:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def fixture_records(fixture_html) -> list[SeaLevelRecord]:
    return parse_html(fixture_html, STATION_ID, STATION_NAME, STATION_URL)


def _make_minimal_html(rows: list[tuple]) -> str:
    """Build a minimal station HTML page with the given (year, *msl_values) rows.

    Args:
        rows: List of tuples where first element is year (int) and subsequent
            elements are MSL values (float or the string '-99999') for
            Jan–Dec columns.  Short rows are allowed; missing columns are omitted.

    Returns:
        HTML string with a table whose first header is 'Year' followed by
        Jan–Dec month headers.
    """
    month_headers = "".join(
        f"<th>{m}</th>"
        for m in ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )
    body_rows = ""
    for row in rows:
        year = row[0]
        cells = "".join(f"<td>{v}</td>" for v in row[1:])
        body_rows += f"<tr><td>{year}</td>{cells}</tr>\n"
    return f"""<!DOCTYPE html><html><body>
<h2>8518750 - Test Station, NY</h2>
<table>
<thead><tr><th>Year</th>{month_headers}</tr></thead>
<tbody>{body_rows}</tbody>
</table>
</body></html>"""


class TestRecordCount:
    def test_fixture_yields_34_records(self, fixture_records):
        """Fixture has 3 years × 12 months minus 2 sentinel rows = 34 records."""
        assert len(fixture_records) == 34

    def test_year_coverage(self, fixture_records):
        years = {r.year for r in fixture_records}
        assert years == {2000, 2001, 2002}

    def test_each_year_has_expected_month_count(self, fixture_records):
        counts = {}
        for r in fixture_records:
            counts[r.year] = counts.get(r.year, 0) + 1
        assert counts[2000] == 11  # Jul missing
        assert counts[2001] == 12
        assert counts[2002] == 11  # Mar missing


class TestFieldTypes:
    def test_year_is_int(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.year, int)

    def test_month_is_int(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.month, int)

    def test_month_in_valid_range(self, fixture_records):
        for rec in fixture_records:
            assert 1 <= rec.month <= 12

    def test_mean_sea_level_mm_is_float(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.mean_sea_level_mm, float)

    def test_record_is_sea_level_record_instance(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec, SeaLevelRecord)

    def test_station_id_is_string(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.station_id, str)


class TestMissingDataExclusion:
    def test_2000_july_sentinel_excluded(self, fixture_records):
        """Fixture has -99999 for 2000-Jul; that record must be absent."""
        july_2000 = [r for r in fixture_records if r.year == 2000 and r.month == 7]
        assert july_2000 == []

    def test_2002_march_sentinel_excluded(self, fixture_records):
        """Fixture has -99999 for 2002-Mar; that record must be absent."""
        mar_2002 = [r for r in fixture_records if r.year == 2002 and r.month == 3]
        assert mar_2002 == []

    def test_inline_sentinel_excluded(self):
        html = _make_minimal_html([(2000, -99999, 0.010)])
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        jan = [r for r in records if r.month == 1]
        assert jan == []

    def test_inline_valid_value_included(self):
        html = _make_minimal_html([(2000, 0.025)])
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        assert len(records) == 1
        assert records[0].month == 1

    def test_blank_cell_excluded(self):
        html = _make_minimal_html([(2000, "", 0.010)])
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        jan = [r for r in records if r.month == 1]
        assert jan == []


class TestMslConversion:
    def test_metres_converted_to_mm(self, fixture_records):
        """2000-Jan fixture value is -0.051 m → -51.0 mm."""
        jan_2000 = next(r for r in fixture_records if r.year == 2000 and r.month == 1)
        assert jan_2000.mean_sea_level_mm == pytest.approx(-51.0, rel=1e-4)

    def test_positive_metre_value_converted(self):
        html = _make_minimal_html([(2000, 0.100)])
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        assert records[0].mean_sea_level_mm == pytest.approx(100.0, rel=1e-4)

    def test_negative_metre_value_converted(self):
        html = _make_minimal_html([(2000, -0.050)])
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        assert records[0].mean_sea_level_mm == pytest.approx(-50.0, rel=1e-4)


class TestRecordMetadata:
    def test_station_id_set_on_all_records(self, fixture_records):
        for rec in fixture_records:
            assert rec.station_id == STATION_ID

    def test_station_name_set_on_all_records(self, fixture_records):
        for rec in fixture_records:
            assert rec.station_name == STATION_NAME

    def test_source_url_set_on_all_records(self, fixture_records):
        for rec in fixture_records:
            assert rec.source_url == STATION_URL

    def test_fetch_time_is_iso8601(self, fixture_records):
        for rec in fixture_records:
            datetime.fromisoformat(rec.fetch_time)

    def test_all_records_share_same_fetch_time(self, fixture_html):
        records = parse_html(fixture_html, STATION_ID, STATION_NAME, STATION_URL)
        times = {r.fetch_time for r in records}
        assert len(times) == 1


class TestEdgeCases:
    def test_empty_html_returns_empty_list(self):
        records = parse_html("<html><body></body></html>", STATION_ID, STATION_NAME, STATION_URL)
        assert records == []

    def test_table_without_year_header_skipped(self):
        html = """<html><body>
        <table><thead><tr><th>Date</th><th>Jan</th></tr></thead>
        <tbody><tr><td>2000</td><td>0.010</td></tr></tbody>
        </table></body></html>"""
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        assert records == []

    def test_non_numeric_year_row_skipped(self):
        html = _make_minimal_html([("n/a", 0.010)])
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        assert records == []

    def test_non_numeric_msl_cell_skipped(self):
        html = _make_minimal_html([(2000, "N/A", 0.010)])
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        jan = [r for r in records if r.month == 1]
        assert jan == []

    def test_large_input_all_valid(self):
        """100 years × 12 months of valid data should produce 1200 records."""
        rows = [(1900 + i, *([0.010] * 12)) for i in range(100)]
        html = _make_minimal_html(rows)
        records = parse_html(html, STATION_ID, STATION_NAME, STATION_URL)
        assert len(records) == 1200

    def test_default_record_fields(self):
        rec = SeaLevelRecord()
        assert rec.station_id == ""
        assert rec.station_name == ""
        assert rec.year == 0
        assert rec.month == 0
        assert rec.mean_sea_level_mm == 0.0
        assert rec.source_url == ""
        assert rec.fetch_time == ""


class TestStationNameExtraction:
    def test_extracts_name_from_h2_heading(self):
        html = "<html><body><h2>8518750 - The Battery, New York</h2></body></html>"
        soup = BeautifulSoup(html, "lxml")
        name = _extract_station_name(soup, "8518750", _DEFAULT_STATIONS)
        assert name == "The Battery, New York"

    def test_falls_back_to_dict_when_no_heading(self):
        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        name = _extract_station_name(soup, "8518750", _DEFAULT_STATIONS)
        assert name == "The Battery, New York"

    def test_falls_back_to_station_id_when_not_in_dict(self):
        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        name = _extract_station_name(soup, "9999999", {})
        assert name == "9999999"

    def test_title_tag_does_not_override_heading(self):
        """Title carries 'Station Home Page - 8518750'; heading has the real name.

        The old code matched the title first (document order) and returned the
        bare station ID.  The fix restricts search to h1/h2/h3 only.
        """
        html = (
            "<html><head><title>Station Home Page - 8518750</title></head>"
            "<body><h2>8518750 - The Battery, New York</h2></body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        name = _extract_station_name(soup, "8518750", _DEFAULT_STATIONS)
        assert name == "The Battery, New York"

    def test_title_only_page_falls_back_to_dict(self):
        """When only a title tag is present (no heading), fall back to dict."""
        html = (
            "<html><head><title>Station Home Page - 8518750</title></head>"
            "<body></body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        name = _extract_station_name(soup, "8518750", _DEFAULT_STATIONS)
        assert name == "The Battery, New York"


class TestScrapeStation:
    def test_returns_records_on_success(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.noaa_sea_level.fetch", return_value=fake_resp):
            records = scrape_station(STATION_ID)
        assert len(records) == 34

    def test_station_name_extracted_from_fixture(self, fixture_html):
        """Regression: fixture has a <title> before the <h2>; name must be the
        human-readable label from the heading, not the bare station ID."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.noaa_sea_level.fetch", return_value=fake_resp):
            records = scrape_station(STATION_ID)
        assert all(r.station_name == STATION_NAME for r in records)

    def test_returns_empty_list_on_fetch_error(self):
        with patch(
            "src.scrapers.noaa_sea_level.fetch",
            side_effect=RuntimeError("robots.txt disallows"),
        ):
            records = scrape_station(STATION_ID)
        assert records == []

    def test_uses_correct_url(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.noaa_sea_level.fetch", return_value=fake_resp) as mock_fetch:
            scrape_station(STATION_ID)
        mock_fetch.assert_called_once_with(_station_url(STATION_ID))


class TestScrapeFunction:
    def test_sleeps_between_stations(self, fixture_html):
        """scrape() must sleep at least _MIN_STATION_SLEEP between station fetches."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with (
            patch("src.scrapers.noaa_sea_level.fetch", return_value=fake_resp),
            patch("src.scrapers.noaa_sea_level.time") as mock_time,
        ):
            scrape(station_ids=["8518750", "9414290"])
        sleep_calls = [c.args[0] for c in mock_time.sleep.call_args_list]
        assert any(s >= _MIN_STATION_SLEEP for s in sleep_calls)

    def test_no_sleep_before_first_station(self, fixture_html):
        """No inter-station sleep should occur before the very first fetch."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with (
            patch("src.scrapers.noaa_sea_level.fetch", return_value=fake_resp),
            patch("src.scrapers.noaa_sea_level.time") as mock_time,
        ):
            scrape(station_ids=["8518750"])
        assert mock_time.sleep.call_count == 0

    def test_combines_records_from_multiple_stations(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with (
            patch("src.scrapers.noaa_sea_level.fetch", return_value=fake_resp),
            patch("src.scrapers.noaa_sea_level.time"),
        ):
            records = scrape(station_ids=["8518750", "9414290"])
        assert len(records) == 68  # 34 records × 2 stations

    def test_skips_failing_station_and_continues(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html

        def side_effect(url: str) -> requests.Response:
            if "9414290" in url:
                raise RuntimeError("HTTP 404")
            return fake_resp

        with (
            patch("src.scrapers.noaa_sea_level.fetch", side_effect=side_effect),
            patch("src.scrapers.noaa_sea_level.time"),
        ):
            records = scrape(station_ids=["8518750", "9414290"])
        assert len(records) == 34

    def test_default_station_ids_used_when_none_given(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with (
            patch("src.scrapers.noaa_sea_level.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.noaa_sea_level.time"),
        ):
            scrape()
        assert mock_fetch.call_count == len(_DEFAULT_STATIONS)
