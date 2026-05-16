"""Tests for src/scrapers/tsa_checkpoint_travel.py.

All tests use static HTML fixtures or inline HTML — zero live network calls.
The fixture contains 12 rows yielding 12 valid records.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protos.tsa_checkpoint_travel_pb2 import TsaCheckpointRecord
from src.scrapers.tsa_checkpoint_travel import (
    SOURCE_URL,
    _parse_int,
    fetch_page,
    parse_table,
    scrape,
    upload_to_bigquery,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "tsa_checkpoint_travel.html"
)

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestParseTableHappyPath:
    def test_returns_correct_row_count(self, sample_html):
        records = parse_table(sample_html)
        assert len(records) == 12

    def test_all_dates_are_iso8601(self, sample_html):
        for rec in parse_table(sample_html):
            assert _DATE_PATTERN.match(rec.date), f"Bad date: {rec.date!r}"

    def test_first_row_date_parsed(self, sample_html):
        records = parse_table(sample_html)
        assert records[0].date == "2025-01-01"

    def test_travelers_total_positive(self, sample_html):
        for rec in parse_table(sample_html):
            assert rec.travelers_total > 0, f"travelers_total not positive: {rec.travelers_total}"

    def test_travelers_year_ago_positive(self, sample_html):
        for rec in parse_table(sample_html):
            assert rec.travelers_year_ago > 0, f"travelers_year_ago not positive: {rec.travelers_year_ago}"

    def test_year_ago_column_distinct_from_total(self, sample_html):
        for rec in parse_table(sample_html):
            assert rec.travelers_total != rec.travelers_year_ago, (
                f"travelers_total == travelers_year_ago for {rec.date}: same column parsed twice?"
            )

    def test_first_row_travelers_total_value(self, sample_html):
        records = parse_table(sample_html)
        assert records[0].travelers_total == 2_009_886

    def test_first_row_travelers_year_ago_value(self, sample_html):
        records = parse_table(sample_html)
        assert records[0].travelers_year_ago == 1_914_409

    def test_all_records_are_tsa_instances(self, sample_html):
        for rec in parse_table(sample_html):
            assert isinstance(rec, TsaCheckpointRecord)

    def test_source_url_populated(self, sample_html):
        for rec in parse_table(sample_html):
            assert rec.source_url == SOURCE_URL

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in parse_table(sample_html):
            assert "T" in rec.fetch_time
            assert rec.fetch_time != ""

    def test_comma_formatted_integers_parsed(self, sample_html):
        records = parse_table(sample_html)
        assert records[0].travelers_total == 2_009_886


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestParseTableEdgeCases:
    def test_large_table(self):
        """A table with 50 rows spread across two months must yield 50 records."""
        from datetime import date, timedelta
        start = date(2025, 1, 1)
        rows = "".join(
            f"<tr><td>{(start + timedelta(days=i)).strftime('%-m/%-d/%Y')}</td>"
            f"<td>{1_000_000 + i * 1000}</td><td>{900_000 + i * 1000}</td></tr>"
            for i in range(50)
        )
        html = f"""
        <html><body>
        <table>
          <thead><tr><th>Date</th><th>2025 Traveler Throughput</th><th>2024 Traveler Throughput</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = parse_table(html)
        assert len(records) == 50
        for rec in records:
            assert _DATE_PATTERN.match(rec.date)
            assert rec.travelers_total > 0

    def test_malformed_row_skipped_without_crash(self):
        """Rows with unparseable dates must be skipped; valid rows still returned."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Date</th><th>2025</th><th>2024</th></tr></thead>
          <tbody>
            <tr><td>1/1/2025</td><td>2,000,000</td><td>1,900,000</td></tr>
            <tr><td>NOT A DATE</td><td>1,500,000</td><td>1,400,000</td></tr>
            <tr><td>MALFORMED ROW</td><td>bad</td><td>data</td></tr>
            <tr><td>1/2/2025</td><td>2,100,000</td><td>2,000,000</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = parse_table(html)
        assert len(records) == 2
        assert records[0].date == "2025-01-01"
        assert records[1].date == "2025-01-02"

    def test_row_with_too_few_cells_skipped(self):
        """Rows with fewer than 3 cells must be skipped silently."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Date</th><th>2025</th><th>2024</th></tr></thead>
          <tbody>
            <tr><td>1/1/2025</td><td>2,000,000</td><td>1,900,000</td></tr>
            <tr><td>1/2/2025</td><td>2,100,000</td></tr>
            <tr><td>1/3/2025</td><td>2,200,000</td><td>2,050,000</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = parse_table(html)
        assert len(records) == 2
        assert records[0].date == "2025-01-01"
        assert records[1].date == "2025-01-03"

    def test_no_tbody_falls_back_to_rows(self):
        """Tables without <tbody> must still be parsed correctly."""
        html = """
        <html><body>
        <table>
          <tr><th>Date</th><th>2025</th><th>2024</th></tr>
          <tr><td>3/15/2025</td><td>2,300,000</td><td>2,200,000</td></tr>
          <tr><td>3/16/2025</td><td>2,150,000</td><td>2,050,000</td></tr>
        </table>
        </body></html>
        """
        records = parse_table(html)
        assert len(records) == 2
        assert records[0].date == "2025-03-15"
        assert records[0].travelers_total == 2_300_000


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestParseTableFailureModes:
    def test_empty_html_raises_value_error(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            parse_table("")

    def test_whitespace_only_html_raises_value_error(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            parse_table("   \n\t  ")

    def test_no_table_raises_value_error(self):
        html = "<html><body><p>No data here.</p></body></html>"
        with pytest.raises(ValueError, match="No <table>"):
            parse_table(html)


# ---------------------------------------------------------------------------
# _parse_int unit tests
# ---------------------------------------------------------------------------


class TestParseInt:
    def test_comma_formatted(self):
        assert _parse_int("2,009,886") == 2_009_886

    def test_plain_integer(self):
        assert _parse_int("1914409") == 1_914_409

    def test_dash_returns_zero(self):
        assert _parse_int("-") == 0

    def test_empty_returns_zero(self):
        assert _parse_int("") == 0

    def test_na_returns_zero(self):
        assert _parse_int("N/A") == 0

    def test_double_dash_returns_zero(self):
        assert _parse_int("--") == 0

    def test_nbsp_stripped(self):
        assert _parse_int("1\xa0000") == 1000


# ---------------------------------------------------------------------------
# fetch_page() tests (no live network)
# ---------------------------------------------------------------------------


class TestFetchPage:
    def test_fetch_page_calls_fetch_with_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.tsa_checkpoint_travel.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.tsa_checkpoint_travel.time.sleep"),
        ):
            html = fetch_page(SOURCE_URL)

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert html == sample_html

    def test_fetch_page_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.tsa_checkpoint_travel.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.tsa_checkpoint_travel.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            fetch_page(SOURCE_URL)

        assert any(s >= 3 for s in sleep_calls), f"No sleep >=3s; calls: {sleep_calls}"


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_returns_tsa_records(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.tsa_checkpoint_travel.fetch", return_value=fake_resp),
            patch("src.scrapers.tsa_checkpoint_travel.time.sleep"),
        ):
            records = scrape()

        assert len(records) > 0
        assert all(isinstance(r, TsaCheckpointRecord) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>no tables</p></body></html>"

        with (
            patch("src.scrapers.tsa_checkpoint_travel.fetch", return_value=fake_resp),
            patch("src.scrapers.tsa_checkpoint_travel.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()


# ---------------------------------------------------------------------------
# upload_to_bigquery stub test
# ---------------------------------------------------------------------------


class TestUploadToBigquery:
    def test_stub_returns_record_count(self, sample_html):
        records = parse_table(sample_html)
        result = upload_to_bigquery(records)
        assert result == len(records)

    def test_stub_empty_list(self):
        assert upload_to_bigquery([]) == 0
