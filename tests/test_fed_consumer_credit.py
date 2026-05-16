"""Tests for src/scrapers/fed_consumer_credit.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.fed_consumer_credit import (
    SOURCE_URL,
    _is_seasonally_adjusted,
    _parse_month_year,
    _record_to_proto,
    run,
    scrape,
)
from protos.fed_consumer_credit_pb2 import FedConsumerCreditRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fed_consumer_credit.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_twelve_records(self, sample_html):
        """3 series × 2 dates × 2 tables (SA + NSA) = 12 records."""
        assert len(run(sample_html)) == 12

    def test_series_names_include_total(self, sample_html):
        names = {r["series_name"] for r in run(sample_html)}
        assert "Total" in names

    def test_series_names_include_revolving(self, sample_html):
        names = {r["series_name"] for r in run(sample_html)}
        assert "Revolving" in names

    def test_series_names_include_non_revolving(self, sample_html):
        names = {r["series_name"] for r in run(sample_html)}
        assert "Non-revolving" in names

    def test_release_dates_are_iso8601(self, sample_html):
        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for r in run(sample_html):
            assert iso_pattern.match(r["release_date"]), f"Bad date: {r['release_date']}"

    def test_amounts_are_positive_floats(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["amount_billions_usd"], float)
            assert r["amount_billions_usd"] > 0

    def test_seasonally_adjusted_flag_splits_evenly(self, sample_html):
        """Fixture has one SA table and one NSA table — 6 records each."""
        records = run(sample_html)
        sa = [r for r in records if r["seasonally_adjusted"]]
        nsa = [r for r in records if not r["seasonally_adjusted"]]
        assert len(sa) == 6
        assert len(nsa) == 6

    def test_seasonally_adjusted_true_for_sa_table(self, sample_html):
        sa_records = [r for r in run(sample_html) if r["seasonally_adjusted"]]
        assert all(r["seasonally_adjusted"] for r in sa_records)

    def test_seasonally_adjusted_false_for_nsa_table(self, sample_html):
        nsa_records = [r for r in run(sample_html) if not r["seasonally_adjusted"]]
        assert all(not r["seasonally_adjusted"] for r in nsa_records)

    def test_two_distinct_dates(self, sample_html):
        dates = {r["release_date"] for r in run(sample_html)}
        assert len(dates) == 2

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_numeric_conversion_precision(self, sample_html):
        """Spot-check a known value from the fixture."""
        sa_total = [
            r for r in run(sample_html)
            if r["series_name"] == "Total" and r["seasonally_adjusted"]
        ]
        amounts = {r["amount_billions_usd"] for r in sa_total}
        assert 5082.5 in amounts
        assert 5094.3 in amounts


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError, match="No consumer credit records"):
            run("")

    def test_no_table_raises_value_error(self):
        html = "<html><body><p>No table here</p></body></html>"
        with pytest.raises(ValueError):
            run(html)

    def test_table_without_date_columns_raises_value_error(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>Series</th><th>Not a date</th></tr></thead>
          <tbody><tr><td>Total</td><td>5000.0</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_commas_in_amounts_parsed_correctly(self):
        """Amounts with thousands separators like '5,082.5' must parse correctly."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Series</th><th>January 2025</th></tr></thead>
          <tbody><tr><td>Total</td><td>5,082.5</td></tr></tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert records[0]["amount_billions_usd"] == 5082.5

    def test_large_table_all_records_valid(self):
        """5 series × 3 date columns = 15 records, all with positive amounts."""
        months = ["January", "February", "March"]
        date_headers = "".join(f"<th>{m} 2025</th>" for m in months)
        data_rows = "".join(
            f"<tr><td>Series {j}</td>"
            + "".join(f"<td>{1000.0 + j * 10 + i:.1f}</td>" for i in range(3))
            + "</tr>"
            for j in range(5)
        )
        html = f"""
        <html><body>
        <h2>Seasonally Adjusted</h2>
        <table>
          <thead><tr><th>Series</th>{date_headers}</tr></thead>
          <tbody>{data_rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 15
        for r in records:
            assert r["amount_billions_usd"] > 0


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_value_error_on_empty_tbody(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>Series</th><th>January 2025</th></tr></thead>
          <tbody></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_unparseable_amount_cell_skipped(self):
        """A cell with non-numeric content is skipped; other valid cells are kept."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Series</th><th>January 2025</th><th>February 2025</th></tr></thead>
          <tbody>
            <tr><td>Total</td><td>n/a</td><td>5082.5</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["amount_billions_usd"] == 5082.5

    def test_empty_series_name_row_skipped(self):
        """Rows where the series cell is blank must not produce records."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Series</th><th>January 2025</th></tr></thead>
          <tbody>
            <tr><td></td><td>5000.0</td></tr>
            <tr><td>Total</td><td>5082.5</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_name"] == "Total"


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class TestParseMonthYear:
    def test_full_month_name(self):
        assert _parse_month_year("January 2025") == "2025-01-01"

    def test_embedded_in_text(self):
        assert _parse_month_year("Data for February 2025") == "2025-02-01"

    def test_returns_none_on_no_match(self):
        assert _parse_month_year("not-a-date") is None

    def test_december(self):
        assert _parse_month_year("December 2024") == "2024-12-01"

    def test_case_insensitive(self):
        assert _parse_month_year("MARCH 2025") == "2025-03-01"


class TestIsSeasonallyAdjusted:
    def test_caption_seasonally_adjusted(self):
        html = """
        <table>
          <caption>Seasonally adjusted annual rates, billions of dollars</caption>
          <thead><tr><th>Series</th></tr></thead>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _is_seasonally_adjusted(table) is True

    def test_caption_not_seasonally_adjusted(self):
        html = """
        <table>
          <caption>Not seasonally adjusted, billions of dollars</caption>
          <thead><tr><th>Series</th></tr></thead>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _is_seasonally_adjusted(table) is False

    def test_preceding_heading_seasonally_adjusted(self):
        html = """
        <div>
          <h2>Seasonally Adjusted</h2>
          <table><thead><tr><th>Series</th></tr></thead></table>
        </div>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _is_seasonally_adjusted(table) is True

    def test_preceding_heading_not_seasonally_adjusted(self):
        html = """
        <div>
          <h2>Not Seasonally Adjusted</h2>
          <table><thead><tr><th>Series</th></tr></thead></table>
        </div>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _is_seasonally_adjusted(table) is False

    def test_default_false_when_no_label(self):
        html = """
        <table><thead><tr><th>Series</th></tr></thead></table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _is_seasonally_adjusted(table) is False


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record from fixture"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, FedConsumerCreditRecord)
        assert msg.release_date == records[0]["release_date"]
        assert msg.series_name == records[0]["series_name"]
        assert msg.amount_billions_usd == records[0]["amount_billions_usd"]
        assert msg.seasonally_adjusted == records[0]["seasonally_adjusted"]
        assert msg.source_url == records[0]["source_url"]
        assert msg.fetch_time != ""

    def test_seasonally_adjusted_proto_bool_true(self, sample_html):
        sa_records = [r for r in run(sample_html) if r["seasonally_adjusted"]]
        msg = _record_to_proto(sa_records[0])
        assert msg.seasonally_adjusted is True

    def test_not_seasonally_adjusted_proto_bool_false(self, sample_html):
        nsa_records = [r for r in run(sample_html) if not r["seasonally_adjusted"]]
        msg = _record_to_proto(nsa_records[0])
        assert msg.seasonally_adjusted is False


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fed_consumer_credit.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.fed_consumer_credit.time.sleep"):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with patch("src.scrapers.fed_consumer_credit.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_consumer_credit.time.sleep",
                   side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_propagates_value_error(self):
        """scrape() must raise ValueError when the page yields no records."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body>no tables</body></html>"

        with patch("src.scrapers.fed_consumer_credit.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_consumer_credit.time.sleep"):
            with pytest.raises(ValueError):
                scrape()

    def test_scrape_returns_same_as_run(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fed_consumer_credit.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_consumer_credit.time.sleep"):
            scraped = scrape()

        direct = run(sample_html)
        assert scraped == direct
