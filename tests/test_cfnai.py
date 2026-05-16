"""Tests for src/scrapers/cfnai.py.

All tests use the static fixture at tests/fixtures/cfnai.html or inline
HTML — zero live network calls.  The fixture contains 12 monthly rows
(2024-01 through 2024-12) including one N/A value in cfnai_ma3 for
2024-12 to exercise null-cell handling.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protos.cfnai_pb2 import CfnaiRecord
from src.scrapers.cfnai import (
    SOURCE_URL,
    _data_rows,
    _parse_date,
    _parse_float,
    run,
    scrape,
)
from bs4 import BeautifulSoup

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "cfnai.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_twelve_records(self, sample_html):
        records = run(sample_html)
        assert len(records) == 12

    def test_all_records_are_cfnai_instances(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec, CfnaiRecord)

    def test_series_date_format_is_yyyy_mm_dd(self, sample_html):
        """YYYY-MM table values must become YYYY-MM-DD (first day of month)."""
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-01$")
        for rec in run(sample_html):
            assert pattern.match(rec.series_date), (
                f"Bad series_date: {rec.series_date!r}"
            )

    def test_first_record_date_is_2024_01_01(self, sample_html):
        records = run(sample_html)
        assert records[0].series_date == "2024-01-01"

    def test_last_record_date_is_2024_12_01(self, sample_html):
        records = run(sample_html)
        assert records[-1].series_date == "2024-12-01"

    def test_positive_cfnai_parsed(self, sample_html):
        """First record has cfnai=0.18 (positive float)."""
        records = run(sample_html)
        assert records[0].cfnai == pytest.approx(0.18)

    def test_negative_cfnai_parsed(self, sample_html):
        """February 2024 has cfnai=-0.37 (negative float)."""
        records = run(sample_html)
        feb = next(r for r in records if r.series_date == "2024-02-01")
        assert feb.cfnai == pytest.approx(-0.37)

    def test_cfnai_ma3_extracted(self, sample_html):
        """October 2024 MA3 should be 0.03."""
        records = run(sample_html)
        oct_rec = next(r for r in records if r.series_date == "2024-10-01")
        assert oct_rec.cfnai_ma3 == pytest.approx(0.03)

    def test_cfnai_ma3_na_becomes_none(self, sample_html):
        """December 2024 has N/A for cfnai_ma3; must produce None."""
        records = run(sample_html)
        dec = next(r for r in records if r.series_date == "2024-12-01")
        assert dec.cfnai_ma3 is None

    def test_negative_sub_index_production(self, sample_html):
        """February 2024 production_and_income=-0.20 (negative sub-index)."""
        records = run(sample_html)
        feb = next(r for r in records if r.series_date == "2024-02-01")
        assert feb.production_and_income == pytest.approx(-0.20)

    def test_negative_sub_index_employment(self, sample_html):
        """July 2024 employment_unemployment_hours=-0.08."""
        records = run(sample_html)
        jul = next(r for r in records if r.series_date == "2024-07-01")
        assert jul.employment_unemployment_hours == pytest.approx(-0.08)

    def test_all_nine_proto_fields_populated(self, sample_html):
        """Every record must have all nine CfnaiRecord fields set."""
        for rec in run(sample_html):
            assert rec.series_date != "", f"Empty series_date: {rec}"
            assert rec.source_url != "", f"Empty source_url: {rec}"
            assert rec.fetch_time != "", f"Empty fetch_time: {rec}"
            # cfnai is not None for all rows except N/A rows
            if rec.series_date != "2024-12-01":
                assert rec.cfnai is not None, f"None cfnai on {rec.series_date}"
                assert rec.production_and_income is not None
                assert rec.employment_unemployment_hours is not None
                assert rec.personal_consumption_housing is not None
                assert rec.sales_orders_inventories is not None

    def test_source_url_stored(self, sample_html):
        for rec in run(sample_html, source_url="https://example.com"):
            assert rec.source_url == "https://example.com"

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in run(sample_html):
            assert "T" in rec.fetch_time
            assert rec.fetch_time != ""


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_first_table_fallback_when_no_id(self):
        """Parser falls back to first <table> when id="cfnai-data" is absent."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th>Date</th><th>CFNAI</th><th>CFNAI-MA3</th>
              <th>Production</th><th>Employment</th><th>Consumption</th><th>Sales</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>2024-03</td><td>0.15</td><td>0.05</td>
              <td>0.08</td><td>0.06</td><td>-0.01</td><td>0.02</td>
            </tr>
            <tr>
              <td>2024-04</td><td>-0.10</td><td>0.00</td>
              <td>-0.06</td><td>-0.03</td><td>-0.01</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-05</td><td>0.07</td><td>0.04</td>
              <td>0.04</td><td>0.02</td><td>0.00</td><td>0.01</td>
            </tr>
            <tr>
              <td>2024-06</td><td>-0.22</td><td>-0.08</td>
              <td>-0.14</td><td>-0.07</td><td>-0.01</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-07</td><td>0.11</td><td>-0.08</td>
              <td>0.07</td><td>0.04</td><td>-0.01</td><td>0.01</td>
            </tr>
            <tr>
              <td>2024-08</td><td>0.03</td><td>-0.03</td>
              <td>0.02</td><td>0.01</td><td>0.00</td><td>0.00</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 6
        assert records[0].series_date == "2024-03-01"
        assert records[1].cfnai == pytest.approx(-0.10)

    def test_various_na_spellings_produce_none(self):
        """N/A, NA, and empty string cells all produce None."""
        html = """
        <html><body>
        <table id="cfnai-data">
          <thead>
            <tr>
              <th>Date</th><th>CFNAI</th><th>CFNAI-MA3</th>
              <th>Production</th><th>Employment</th><th>Consumption</th><th>Sales</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>2024-06</td><td>N/A</td><td>NA</td>
              <td></td><td>-0.03</td><td>n/a</td><td>0.01</td>
            </tr>
            <tr>
              <td>2024-07</td><td>0.05</td><td>0.02</td>
              <td>0.03</td><td>0.02</td><td>0.00</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-08</td><td>-0.12</td><td>-0.04</td>
              <td>-0.07</td><td>-0.04</td><td>-0.01</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-09</td><td>0.18</td><td>0.04</td>
              <td>0.10</td><td>0.07</td><td>-0.01</td><td>0.02</td>
            </tr>
            <tr>
              <td>2024-10</td><td>-0.05</td><td>0.00</td>
              <td>-0.03</td><td>-0.01</td><td>-0.01</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-11</td><td>0.22</td><td>0.12</td>
              <td>0.13</td><td>0.08</td><td>-0.01</td><td>0.02</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        na_row = records[0]
        assert na_row.cfnai is None
        assert na_row.cfnai_ma3 is None
        assert na_row.production_and_income is None
        assert na_row.personal_consumption_housing is None

    def test_short_rows_skipped_silently(self):
        """Rows with fewer than 7 cells are skipped without raising."""
        html = """
        <html><body>
        <table id="cfnai-data">
          <thead>
            <tr>
              <th>Date</th><th>CFNAI</th><th>MA3</th>
              <th>Prod</th><th>Empl</th><th>Consump</th><th>Sales</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>2024-05</td><td>0.12</td></tr>
            <tr>
              <td>2024-06</td><td>0.15</td><td>0.08</td>
              <td>0.09</td><td>0.05</td><td>-0.01</td><td>0.02</td>
            </tr>
            <tr>
              <td>2024-07</td><td>-0.10</td><td>0.02</td>
              <td>-0.06</td><td>-0.03</td><td>-0.01</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-08</td><td>0.08</td><td>0.04</td>
              <td>0.05</td><td>0.03</td><td>0.00</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-09</td><td>-0.15</td><td>-0.06</td>
              <td>-0.09</td><td>-0.05</td><td>-0.01</td><td>0.00</td>
            </tr>
            <tr>
              <td>2024-10</td><td>0.21</td><td>0.05</td>
              <td>0.12</td><td>0.08</td><td>-0.01</td><td>0.02</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 5
        assert all(r.series_date != "2024-05-01" for r in records)

    def test_large_table_all_rows_parsed(self):
        """A table with 24 data rows yields exactly 24 records."""
        rows_html = "".join(
            f"<tr>"
            f"<td>20{22 + i // 12:02d}-{(i % 12) + 1:02d}</td>"
            f"<td>{0.1 * (i % 5 - 2):.2f}</td>"
            f"<td>{0.05 * (i % 4 - 1):.2f}</td>"
            f"<td>{0.04 * (i % 3 - 1):.2f}</td>"
            f"<td>{0.03 * (i % 3 - 1):.2f}</td>"
            f"<td>{-0.01:.2f}</td>"
            f"<td>{0.01 * (i % 2):.2f}</td>"
            f"</tr>"
            for i in range(24)
        )
        html = f"""
        <html><body>
        <table id="cfnai-data">
          <thead>
            <tr>
              <th>Date</th><th>CFNAI</th><th>MA3</th>
              <th>Prod</th><th>Empl</th><th>Consump</th><th>Sales</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 24


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_empty_html_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("   \n\t  ")

    def test_no_table_raises(self):
        html = "<html><body><p>No table here.</p></body></html>"
        with pytest.raises(ValueError, match="No table found"):
            run(html)

    def test_table_with_no_date_rows_raises(self):
        """A table whose rows have no YYYY-MM dates produces no records and raises."""
        html = """
        <html><body>
        <table id="cfnai-data">
          <thead><tr><th>col1</th><th>col2</th></tr></thead>
          <tbody>
            <tr><td>not-a-date</td><td>0.1</td></tr>
            <tr><td>also-not</td><td>0.2</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)


# ---------------------------------------------------------------------------
# _parse_float unit tests
# ---------------------------------------------------------------------------


class TestParseFloat:
    def test_positive_value(self):
        assert _parse_float("0.18") == pytest.approx(0.18)

    def test_negative_value(self):
        assert _parse_float("-0.37") == pytest.approx(-0.37)

    def test_zero(self):
        assert _parse_float("0.00") == pytest.approx(0.0)

    def test_na_string_returns_none(self):
        assert _parse_float("N/A") is None

    def test_empty_string_returns_none(self):
        assert _parse_float("") is None

    def test_double_dash_returns_none(self):
        assert _parse_float("--") is None

    def test_whitespace_stripped(self):
        assert _parse_float("  0.12  ") == pytest.approx(0.12)

    def test_unparseable_returns_none(self):
        assert _parse_float("n.a.") is None


# ---------------------------------------------------------------------------
# _parse_date unit tests
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_yyyy_mm_to_yyyy_mm_01(self):
        assert _parse_date("2024-01") == "2024-01-01"

    def test_december_date(self):
        assert _parse_date("2024-12") == "2024-12-01"

    def test_whitespace_stripped(self):
        assert _parse_date("  2024-06  ") == "2024-06-01"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            _parse_date("2024/01")

    def test_plain_year_raises(self):
        with pytest.raises(ValueError):
            _parse_date("2024")


# ---------------------------------------------------------------------------
# _data_rows unit tests
# ---------------------------------------------------------------------------


class TestDataRows:
    def test_returns_tbody_rows_when_present(self):
        html = """
        <table>
          <thead><tr><th>Date</th></tr></thead>
          <tbody>
            <tr><td>2024-01</td></tr>
            <tr><td>2024-02</td></tr>
          </tbody>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        rows = _data_rows(table)
        assert len(rows) == 2

    def test_skips_header_row_without_tbody(self):
        html = """
        <table>
          <tr><th>Date</th><th>CFNAI</th></tr>
          <tr><td>2024-05</td><td>0.10</td></tr>
          <tr><td>2024-06</td><td>-0.05</td></tr>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        rows = _data_rows(table)
        assert len(rows) == 2
        first_text = rows[0].find("td").get_text(strip=True)
        assert first_text == "2024-05"


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.cfnai.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.cfnai.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.cfnai.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.cfnai.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >=3s; calls={sleep_calls}"

    def test_scrape_returns_cfnai_records(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.cfnai.fetch", return_value=fake_resp),
            patch("src.scrapers.cfnai.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, CfnaiRecord) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>No table</p></body></html>"

        with (
            patch("src.scrapers.cfnai.fetch", return_value=fake_resp),
            patch("src.scrapers.cfnai.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
