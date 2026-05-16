"""Tests for src/scrapers/fed_money_stock.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.fed_money_stock import (
    SOURCE_URL,
    _find_column_indices,
    _parse_date,
    _record_to_proto,
    main,
    run,
    scrape,
)
from protos.fed_money_stock_pb2 import FedMoneyStockRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fed_h6_sample.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_three_records(self, sample_html):
        """Fixture has 3 data rows and 2 footnote rows; only 3 records expected."""
        assert len(run(sample_html)) == 3

    def test_series_date_is_iso8601(self, sample_html):
        """All series_date values must be YYYY-MM-DD strings."""
        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for r in run(sample_html):
            assert iso_pattern.match(r["series_date"]), f"Bad date: {r['series_date']}"

    def test_m1_values_positive(self, sample_html):
        for r in run(sample_html):
            assert r["m1_seasonally_adjusted_billions"] > 0

    def test_m2_values_positive(self, sample_html):
        for r in run(sample_html):
            assert r["m2_seasonally_adjusted_billions"] > 0

    def test_m2_greater_than_m1(self, sample_html):
        """M2 is a superset of M1 so M2 must always exceed M1."""
        for r in run(sample_html):
            assert r["m2_seasonally_adjusted_billions"] > r["m1_seasonally_adjusted_billions"]

    def test_footnote_rows_skipped(self, sample_html):
        """Asterisk and blank-date rows must not appear in results."""
        dates = {r["series_date"] for r in run(sample_html)}
        assert len(dates) == 3
        for d in dates:
            assert d not in ("", "*")

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_correct_m1_for_first_row(self, sample_html):
        records = sorted(run(sample_html), key=lambda r: r["series_date"])
        assert records[0]["series_date"] == "2026-04-14"
        assert records[0]["m1_seasonally_adjusted_billions"] == pytest.approx(18245.3)

    def test_correct_m2_for_first_row(self, sample_html):
        records = sorted(run(sample_html), key=lambda r: r["series_date"])
        assert records[0]["m2_seasonally_adjusted_billions"] == pytest.approx(21583.7)

    def test_all_required_fields_present(self, sample_html):
        required = {"series_date", "m1_seasonally_adjusted_billions",
                    "m2_seasonally_adjusted_billions", "source_url"}
        for r in run(sample_html):
            assert required.issubset(r.keys()), f"Missing fields in: {r}"


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_html_returns_empty_list(self):
        assert run("") == []

    def test_no_table_returns_empty_list(self):
        assert run("<html><body><p>No table here</p></body></html>") == []

    def test_table_without_m1_m2_headers_skipped(self):
        html = """
        <html><body><table>
          <thead><tr><th>Date</th><th>CPI</th><th>PPI</th></tr></thead>
          <tbody><tr><td>Apr 14, 2026</td><td>1.2</td><td>0.9</td></tr></tbody>
        </table></body></html>
        """
        assert run(html) == []

    def test_double_space_date_parsed_correctly(self):
        """Date strings like 'May  6, 2026' (double space) must parse to YYYY-MM-DD."""
        html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>May  6, 2026</td><td>18400.0</td><td>21750.0</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_date"] == "2026-05-06"

    def test_abbreviated_month_parsed_correctly(self):
        html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Jan 12, 2026</td><td>18100.0</td><td>21400.0</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_date"] == "2026-01-12"

    def test_thousands_separator_stripped(self):
        """Values like '18,245.3' must parse to 18245.3."""
        html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18,245.3</td><td>21,583.7</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["m1_seasonally_adjusted_billions"] == pytest.approx(18245.3)

    def test_large_table_all_rows_extracted(self):
        """Table with 20 data rows must yield 20 records."""
        rows = "".join(
            f"<tr><td>Jan {i + 1:02d}, 2026</td>"
            f"<td>{18000 + i * 10}.0</td>"
            f"<td>{21000 + i * 10}.0</td></tr>"
            for i in range(20)
        )
        html = f"""
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>{rows}</tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 20
        for r in records:
            assert r["m1_seasonally_adjusted_billions"] > 0
            assert r["m2_seasonally_adjusted_billions"] > r["m1_seasonally_adjusted_billions"]


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_asterisk_date_row_skipped(self):
        """Row with '*' in date cell must produce no record."""
        html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
            <tr><td>*</td><td colspan="2">Seasonally adjusted by the Fed.</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_date"] == "2026-04-14"

    def test_blank_date_row_skipped(self):
        """Row with empty date cell must produce no record."""
        html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
            <tr><td></td><td colspan="2">Note: data revised.</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1

    def test_unparseable_m1_row_skipped(self):
        """Row where M1 cell contains non-numeric text must be skipped."""
        html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>n/a</td><td>21583.7</td></tr>
            <tr><td>Apr 21, 2026</td><td>18271.9</td><td>21609.2</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_date"] == "2026-04-21"

    def test_unparseable_date_row_skipped(self):
        """Row with a non-date, non-asterisk string in the date cell is skipped."""
        html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>not-a-date</td><td>18245.3</td><td>21583.7</td></tr>
            <tr><td>Apr 21, 2026</td><td>18271.9</td><td>21609.2</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_date"] == "2026-04-21"

    def test_table_without_thead_skipped(self):
        html = """
        <html><body><table>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
          </tbody>
        </table></body></html>
        """
        assert run(html) == []


# ---------------------------------------------------------------------------
# _parse_date unit tests
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_full_month_name(self):
        assert _parse_date("May 12, 2026") == "2026-05-12"

    def test_abbreviated_month(self):
        assert _parse_date("Apr 28, 2026") == "2026-04-28"

    def test_double_space_before_single_digit_day(self):
        assert _parse_date("May  6, 2026") == "2026-05-06"

    def test_blank_returns_none(self):
        assert _parse_date("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_date("   ") is None

    def test_asterisk_returns_none(self):
        assert _parse_date("*") is None

    def test_asterisk_with_text_returns_none(self):
        assert _parse_date("* Seasonally adjusted") is None

    def test_unrecognised_format_returns_none(self):
        assert _parse_date("2026-04-14") is None

    def test_january_single_digit_day(self):
        assert _parse_date("January 5, 2026") == "2026-01-05"


# ---------------------------------------------------------------------------
# _find_column_indices unit tests
# ---------------------------------------------------------------------------


class TestFindColumnIndices:
    def _make_cells(self, labels: list[str]):
        from bs4 import BeautifulSoup
        html = "<tr>" + "".join(f"<th>{lbl}</th>" for lbl in labels) + "</tr>"
        return BeautifulSoup(html, "lxml").find_all("th")

    def test_simple_m1_m2_headers(self):
        cells = self._make_cells(["Week ending", "M1", "M2"])
        m1, m2 = _find_column_indices(cells)
        assert m1 == 1
        assert m2 == 2

    def test_verbose_headers(self):
        cells = self._make_cells(["Date", "M1 seasonally adjusted", "M2 seasonally adjusted"])
        m1, m2 = _find_column_indices(cells)
        assert m1 == 1
        assert m2 == 2

    def test_missing_m2_returns_none(self):
        cells = self._make_cells(["Date", "M1", "CPI"])
        _, m2 = _find_column_indices(cells)
        assert m2 is None

    def test_missing_m1_returns_none(self):
        cells = self._make_cells(["Date", "CPI", "M2"])
        m1, _ = _find_column_indices(cells)
        assert m1 is None

    def test_both_missing_returns_none_none(self):
        cells = self._make_cells(["Date", "CPI", "PPI"])
        m1, m2 = _find_column_indices(cells)
        assert m1 is None
        assert m2 is None

    def test_first_occurrence_used(self):
        """When M1 appears twice, the first index is returned."""
        cells = self._make_cells(["Date", "M1", "M2", "M1 NSA"])
        m1, _ = _find_column_indices(cells)
        assert m1 == 1


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record from fixture"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, FedMoneyStockRecord)
        assert msg.series_date == records[0]["series_date"]
        assert msg.m1_seasonally_adjusted_billions == pytest.approx(
            records[0]["m1_seasonally_adjusted_billions"]
        )
        assert msg.m2_seasonally_adjusted_billions == pytest.approx(
            records[0]["m2_seasonally_adjusted_billions"]
        )
        assert msg.source_url == records[0]["source_url"]
        assert msg.fetch_time != ""

    def test_fetch_time_is_iso8601(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        assert iso_pattern.match(msg.fetch_time), f"Bad fetch_time: {msg.fetch_time}"


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
          </tbody>
        </table></body></html>
        """
        with patch("src.scrapers.fed_money_stock.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.fed_money_stock.time.sleep"):
            scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)

    def test_scrape_sleeps_at_least_3_seconds(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
          </tbody>
        </table></body></html>
        """
        sleep_calls: list[float] = []
        with patch("src.scrapers.fed_money_stock.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_money_stock.time.sleep",
                   side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_returns_same_as_run(self):
        test_html = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
            <tr><td>Apr 21, 2026</td><td>18271.9</td><td>21609.2</td></tr>
          </tbody>
        </table></body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.fed_money_stock.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_money_stock.time.sleep"):
            scraped = scrape()

        direct = run(test_html)
        assert scraped == direct


# ---------------------------------------------------------------------------
# BigQuery upload integration test
# ---------------------------------------------------------------------------


class TestMainBigQueryUpload:
    def test_main_calls_upload_rows_with_correct_table(self):
        """main() must call upload_rows with table name 'fed_money_stock'."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
            <tr><td>Apr 21, 2026</td><td>18271.9</td><td>21609.2</td></tr>
          </tbody>
        </table></body></html>
        """
        with patch("src.scrapers.fed_money_stock.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_money_stock.time.sleep"), \
             patch("src.scrapers.fed_money_stock.upload_rows", return_value=2) as mock_upload:
            result = main()

        mock_upload.assert_called_once()
        table_arg = mock_upload.call_args[0][0]
        assert table_arg == "fed_money_stock"
        assert result == 2

    def test_main_passes_proto_messages_to_upload(self):
        """main() must pass FedMoneyStockRecord instances to upload_rows."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body><table>
          <thead><tr><th>Week ending</th><th>M1</th><th>M2</th></tr></thead>
          <tbody>
            <tr><td>Apr 14, 2026</td><td>18245.3</td><td>21583.7</td></tr>
          </tbody>
        </table></body></html>
        """
        with patch("src.scrapers.fed_money_stock.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_money_stock.time.sleep"), \
             patch("src.scrapers.fed_money_stock.upload_rows", return_value=1) as mock_upload:
            main()

        messages = mock_upload.call_args[0][1]
        assert len(messages) == 1
        assert isinstance(messages[0], FedMoneyStockRecord)
        assert messages[0].series_date == "2026-04-14"
