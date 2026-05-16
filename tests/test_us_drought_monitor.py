"""Tests for src/scrapers/us_drought_monitor.py.

All tests use the static fixture at tests/fixtures/drought_monitor_stats.html
or inline HTML snippets.  No live network calls are made.
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.us_drought_monitor import (
    REQUIRED_FIELDS,
    SOURCE_URL,
    TOP_LEVEL_REGIONS,
    _build_col_map,
    _find_region_col,
    _normalize_col_header,
    _parse_date,
    _parse_float,
    _record_to_proto,
    run,
    scrape,
)
from protos.us_drought_monitor_pb2 import DroughtRecord
from bs4 import BeautifulSoup

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "drought_monitor_stats.html"
)

_INLINE_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>MapDate</th>
      <th>Name</th>
      <th>None</th>
      <th>D0</th>
      <th>D1</th>
      <th>D2</th>
      <th>D3</th>
      <th>D4</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>1/2/2024</td><td>CONUS</td><td>74.64</td><td>12.75</td><td>7.33</td><td>3.22</td><td>1.58</td><td>0.48</td></tr>
    <tr><td>1/2/2024</td><td>Alaska</td><td>92.34</td><td>5.10</td><td>2.56</td><td>0.00</td><td>0.00</td><td>0.00</td></tr>
    <tr><td>1/2/2024</td><td>Alabama</td><td>100.00</td><td>0.00</td><td>0.00</td><td>0.00</td><td>0.00</td><td>0.00</td></tr>
    <tr><td>1/9/2024</td><td>CONUS</td><td>73.21</td><td>13.44</td><td>7.89</td><td>3.61</td><td>1.65</td><td>0.20</td></tr>
    <tr><td>1/9/2024</td><td>Hawaii</td><td>61.20</td><td>18.45</td><td>12.33</td><td>5.22</td><td>2.80</td><td>0.00</td></tr>
    <tr><td>1/9/2024</td><td>Puerto Rico</td><td>88.50</td><td>7.20</td><td>3.80</td><td>0.50</td><td>0.00</td><td>0.00</td></tr>
  </tbody>
</table>
</body></html>
"""

_NA_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>MapDate</th><th>Name</th><th>None</th>
      <th>D0</th><th>D1</th><th>D2</th><th>D3</th><th>D4</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>1/2/2024</td><td>CONUS</td><td>N/A</td><td>N/A</td><td>N/A</td><td>N/A</td><td>N/A</td><td>N/A</td></tr>
    <tr><td>1/9/2024</td><td>CONUS</td><td>73.21</td><td>13.44</td><td>7.89</td><td>3.61</td><td>1.65</td><td>0.20</td></tr>
  </tbody>
</table>
</body></html>
"""

_LONG_HEADERS_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>MapDate</th>
      <th>Name</th>
      <th>None</th>
      <th>D0 - Abnormally Dry</th>
      <th>D1 - Moderate Drought</th>
      <th>D2 - Severe Drought</th>
      <th>D3 - Extreme Drought</th>
      <th>D4 - Exceptional Drought</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>3/5/2024</td><td>CONUS</td><td>63.90</td><td>19.30</td><td>11.00</td><td>3.80</td><td>2.00</td><td>0.30</td></tr>
    <tr><td>3/5/2024</td><td>alaska</td><td>89.00</td><td>8.00</td><td>3.00</td><td>0.00</td><td>0.00</td><td>0.00</td></tr>
  </tbody>
</table>
</body></html>
"""


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestNormalizeColHeader:
    def test_plain_label_unchanged(self):
        assert _normalize_col_header("D0") == "D0"

    def test_long_form_stripped(self):
        assert _normalize_col_header("D0 - Abnormally Dry") == "D0"

    def test_moderate_drought_stripped(self):
        assert _normalize_col_header("D1 - Moderate Drought") == "D1"

    def test_mapdate_unchanged(self):
        assert _normalize_col_header("MapDate") == "MapDate"

    def test_name_unchanged(self):
        assert _normalize_col_header("Name") == "Name"


class TestParseDate:
    def test_slash_format_parsed(self):
        assert _parse_date("1/2/2024") == "2024-01-02"

    def test_slash_format_double_digits(self):
        assert _parse_date("12/31/2024") == "2024-12-31"

    def test_iso_format_accepted(self):
        assert _parse_date("2024-01-02") == "2024-01-02"

    def test_invalid_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_date("") is None


class TestParseFloat:
    def test_numeric_string(self):
        assert _parse_float("12.75") == pytest.approx(12.75)

    def test_zero(self):
        assert _parse_float("0.00") == pytest.approx(0.0)

    def test_na_returns_none(self):
        assert _parse_float("N/A") is None

    def test_na_uppercase_returns_none(self):
        assert _parse_float("N/A") is None

    def test_empty_returns_none(self):
        assert _parse_float("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_float("   ") is None

    def test_non_numeric_returns_none(self):
        assert _parse_float("abc") is None


class TestBuildColMap:
    def _cells(self, headers: list[str]):
        html = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
        return BeautifulSoup(html, "html.parser").find_all("th")

    def test_plain_headers_mapped(self):
        cells = self._cells(["MapDate", "Name", "D0", "D1"])
        result = _build_col_map(cells)
        assert result == {"MapDate": 0, "Name": 1, "D0": 2, "D1": 3}

    def test_long_form_headers_normalised(self):
        cells = self._cells(["MapDate", "Name", "D0 - Abnormally Dry"])
        result = _build_col_map(cells)
        assert "D0" in result
        assert result["D0"] == 2


class TestFindRegionCol:
    def test_name_header_found(self):
        assert _find_region_col({"MapDate": 0, "Name": 1, "D0": 2}) == 1

    def test_state_header_found(self):
        assert _find_region_col({"MapDate": 0, "State": 1}) == 1

    def test_region_header_found(self):
        assert _find_region_col({"MapDate": 0, "Region": 2}) == 2

    def test_no_known_header_returns_none(self):
        assert _find_region_col({"MapDate": 0, "D0": 1}) is None


class TestRunHappyPath:
    def test_fixture_returns_records(self, sample_html):
        assert len(run(sample_html)) > 0

    def test_fixture_has_at_least_ten_conus_rows(self, sample_html):
        conus = [r for r in run(sample_html) if r["region"] == "conus"]
        assert len(conus) >= 10

    def test_release_date_is_yyyy_mm_dd(self, sample_html):
        for r in run(sample_html):
            datetime.strptime(r["release_date"], "%Y-%m-%d")

    def test_d_values_are_floats(self, sample_html):
        for r in run(sample_html):
            for field in ("d0_percent", "d1_percent", "d2_percent", "d3_percent", "d4_percent"):
                assert isinstance(r[field], float), f"{field} not float in {r}"

    def test_d0_through_d4_sum_le_100(self, sample_html):
        for r in run(sample_html):
            total = r["d0_percent"] + r["d1_percent"] + r["d2_percent"] + r["d3_percent"] + r["d4_percent"]
            assert total <= 100.0 + 1e-6, f"Sum {total} > 100 in {r}"

    def test_source_url_in_every_record(self, sample_html):
        for r in run(sample_html):
            assert r["source_url"] == SOURCE_URL

    def test_specific_conus_date_and_d0(self, sample_html):
        records = run(sample_html)
        rec = next(
            (r for r in records if r["release_date"] == "2024-01-02" and r["region"] == "conus"),
            None,
        )
        assert rec is not None
        assert rec["d0_percent"] == pytest.approx(12.75)

    def test_specific_conus_all_d_levels(self, sample_html):
        records = run(sample_html)
        rec = next(
            (r for r in records if r["release_date"] == "2024-01-02" and r["region"] == "conus"),
            None,
        )
        assert rec is not None
        assert rec["d1_percent"] == pytest.approx(7.33)
        assert rec["d2_percent"] == pytest.approx(3.22)
        assert rec["d3_percent"] == pytest.approx(1.58)
        assert rec["d4_percent"] == pytest.approx(0.48)

    def test_alaska_row_parsed(self, sample_html):
        records = run(sample_html)
        alaska = [r for r in records if r["region"] == "alaska"]
        assert len(alaska) >= 1

    def test_hawaii_row_parsed(self, sample_html):
        records = run(sample_html)
        hawaii = [r for r in records if r["region"] == "hawaii"]
        assert len(hawaii) >= 1

    def test_puerto_rico_row_parsed(self, sample_html):
        records = run(sample_html)
        pr = [r for r in records if r["region"] == "puerto rico"]
        assert len(pr) >= 1

    def test_inline_conus_date(self):
        records = run(_INLINE_HTML)
        rec = next(
            (r for r in records if r["region"] == "conus" and r["release_date"] == "2024-01-02"),
            None,
        )
        assert rec is not None
        assert rec["d0_percent"] == pytest.approx(12.75)

    def test_long_form_headers_parsed(self):
        records = run(_LONG_HEADERS_HTML)
        assert len(records) == 2
        conus = next(r for r in records if r["region"] == "conus")
        assert conus["d0_percent"] == pytest.approx(19.30)
        assert conus["d4_percent"] == pytest.approx(0.30)


class TestRequiredFields:
    def test_all_required_fields_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for r in run(sample_html):
            assert required.issubset(r.keys()), f"Missing keys in {r}"

    def test_field_types(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["release_date"], str)
            assert isinstance(r["region"], str)
            assert isinstance(r["source_url"], str)
            for pct_field in ("d0_percent", "d1_percent", "d2_percent", "d3_percent", "d4_percent"):
                assert isinstance(r[pct_field], float)


class TestEdgeCases:
    def test_empty_html_returns_empty_list(self):
        assert run("") == []

    def test_no_table_returns_empty_list(self):
        assert run("<html><body><p>No data here.</p></body></html>") == []

    def test_table_without_mapdate_header_skipped(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>Date</th><th>Name</th><th>D0</th><th>D1</th><th>D2</th><th>D3</th><th>D4</th></tr></thead>
          <tbody><tr><td>1/2/2024</td><td>CONUS</td><td>10</td><td>5</td><td>2</td><td>1</td><td>0</td></tr></tbody>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_na_rows_skipped_without_raising(self):
        records = run(_NA_HTML)
        assert all(r["release_date"] == "2024-01-09" for r in records)
        assert len(records) == 1

    def test_empty_d4_cell_skipped(self, sample_html):
        records = run(sample_html)
        dates = {r["release_date"] for r in records if r["region"] == "conus"}
        assert "2024-02-27" not in dates

    def test_state_rows_filtered_out(self):
        records = run(_INLINE_HTML)
        regions = {r["region"] for r in records}
        for region in regions:
            assert region in TOP_LEVEL_REGIONS, f"Non-top-level region emitted: {region}"
        assert "alabama" not in regions

    def test_large_input_all_valid(self):
        """200 CONUS rows should all parse correctly."""
        rows = "".join(
            f"<tr><td>1/{(i % 28) + 1}/2024</td><td>CONUS</td><td>50.00</td>"
            f"<td>20.00</td><td>10.00</td><td>5.00</td><td>2.00</td><td>1.00</td></tr>"
            for i in range(200)
        )
        html = f"""
        <html><body>
        <table>
          <thead>
            <tr><th>MapDate</th><th>Name</th><th>None</th><th>D0</th><th>D1</th><th>D2</th><th>D3</th><th>D4</th></tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 200
        for r in records:
            total = r["d0_percent"] + r["d1_percent"] + r["d2_percent"] + r["d3_percent"] + r["d4_percent"]
            assert total <= 100.0 + 1e-6

    def test_mixed_case_region_normalised_to_lowercase(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>MapDate</th><th>Name</th><th>None</th><th>D0</th><th>D1</th><th>D2</th><th>D3</th><th>D4</th></tr></thead>
          <tbody>
            <tr><td>1/2/2024</td><td>CONUS</td><td>74.00</td><td>12.00</td><td>7.00</td><td>3.00</td><td>2.00</td><td>1.00</td></tr>
            <tr><td>1/2/2024</td><td>Alaska</td><td>90.00</td><td>6.00</td><td>3.00</td><td>1.00</td><td>0.00</td><td>0.00</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert records[0]["region"] == "conus"
        assert records[1]["region"] == "alaska"


class TestFailureModes:
    def test_na_in_d0_skips_entire_row(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>MapDate</th><th>Name</th><th>None</th><th>D0</th><th>D1</th><th>D2</th><th>D3</th><th>D4</th></tr></thead>
          <tbody>
            <tr><td>1/2/2024</td><td>CONUS</td><td>N/A</td><td>N/A</td><td>7.00</td><td>3.00</td><td>2.00</td><td>1.00</td></tr>
            <tr><td>1/9/2024</td><td>CONUS</td><td>73.00</td><td>13.00</td><td>8.00</td><td>3.00</td><td>2.00</td><td>0.50</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["release_date"] == "2024-01-09"

    def test_completely_empty_d_cell_skips_row(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>MapDate</th><th>Name</th><th>None</th><th>D0</th><th>D1</th><th>D2</th><th>D3</th><th>D4</th></tr></thead>
          <tbody>
            <tr><td>2/27/2024</td><td>CONUS</td><td>65.20</td><td>18.60</td><td>10.50</td><td>4.20</td><td>1.30</td><td></td></tr>
          </tbody>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_non_top_level_region_never_emitted(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>MapDate</th><th>Name</th><th>None</th><th>D0</th><th>D1</th><th>D2</th><th>D3</th><th>D4</th></tr></thead>
          <tbody>
            <tr><td>1/2/2024</td><td>California</td><td>45.00</td><td>22.00</td><td>18.00</td><td>10.00</td><td>4.00</td><td>0.00</td></tr>
            <tr><td>1/2/2024</td><td>Texas</td><td>58.00</td><td>19.00</td><td>14.00</td><td>6.00</td><td>1.00</td><td>0.00</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        assert run(html) == []


class TestProtoFields:
    def test_record_to_proto_populates_all_fields(self):
        record = {
            "release_date": "2024-01-02",
            "region": "conus",
            "d0_percent": 12.75,
            "d1_percent": 7.33,
            "d2_percent": 3.22,
            "d3_percent": 1.58,
            "d4_percent": 0.48,
            "source_url": SOURCE_URL,
        }
        msg = _record_to_proto(record)
        assert isinstance(msg, DroughtRecord)
        assert msg.release_date == "2024-01-02"
        assert msg.region == "conus"
        assert msg.d0_percent == pytest.approx(12.75)
        assert msg.d1_percent == pytest.approx(7.33)
        assert msg.d2_percent == pytest.approx(3.22)
        assert msg.d3_percent == pytest.approx(1.58)
        assert msg.d4_percent == pytest.approx(0.48)
        assert msg.source_url == SOURCE_URL

    def test_fetch_time_populated(self):
        record = {
            "release_date": "2024-01-02",
            "region": "alaska",
            "d0_percent": 5.10,
            "d1_percent": 2.56,
            "d2_percent": 0.00,
            "d3_percent": 0.00,
            "d4_percent": 0.00,
            "source_url": SOURCE_URL,
        }
        msg = _record_to_proto(record)
        assert msg.fetch_time is not None
        assert msg.fetch_time._dt is not None

    def test_proto_dataclass_defaults(self):
        msg = DroughtRecord()
        assert msg.release_date == ""
        assert msg.region == ""
        assert msg.d0_percent == 0.0
        assert msg.d1_percent == 0.0
        assert msg.d2_percent == 0.0
        assert msg.d3_percent == 0.0
        assert msg.d4_percent == 0.0
        assert msg.source_url == ""


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_url_containing_dates(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.us_drought_monitor.fetch", return_value=fake_resp) as mock_fetch:
            with patch("src.scrapers.us_drought_monitor.time.sleep"):
                records = scrape(startdate="2024-01-01", enddate="2024-03-31")
        call_url = mock_fetch.call_args[0][0]
        assert "startdate=2024-01-01" in call_url
        assert "enddate=2024-03-31" in call_url
        assert "statType=0" in call_url
        assert len(records) > 0

    def test_scrape_returns_same_as_run_with_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.us_drought_monitor.fetch", return_value=fake_resp) as mock_fetch:
            with patch("src.scrapers.us_drought_monitor.time.sleep"):
                scraped = scrape(startdate="2024-01-01", enddate="2024-03-31")
        called_url = mock_fetch.call_args[0][0]
        expected = run(_INLINE_HTML, source_url=called_url)
        assert scraped == expected

    def test_scrape_sleeps_after_fetch(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.us_drought_monitor.fetch", return_value=fake_resp):
            with patch("src.scrapers.us_drought_monitor.time.sleep") as mock_sleep:
                scrape()
        mock_sleep.assert_called_once_with(3)

    def test_scrape_no_live_network(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.us_drought_monitor.fetch", return_value=fake_resp) as mock_fetch:
            with patch("src.scrapers.us_drought_monitor.time.sleep"):
                scrape()
        assert mock_fetch.call_count == 1
