"""Tests for src/scrapers/fdic_bank_failures.py.

All tests use a static HTML fixture or inline HTML — zero live network calls.
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.fdic_bank_failures import (
    SOURCE_URL,
    _parse_cert,
    _parse_date,
    _record_to_proto,
    run,
    scrape,
)
from protos.fdic_bank_failures_pb2 import FdicBankFailureRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fdic_bank_failures.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_twelve_records(self, sample_html):
        """Fixture has 12 data rows; one record per bank failure."""
        assert len(run(sample_html)) == 12

    def test_cert_fields_are_integers(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["cert"], int), f"cert not int: {r['cert']!r}"

    def test_failure_dates_are_valid_iso8601(self, sample_html):
        """failure_date must parse as a real calendar date in YYYY-MM-DD format."""
        for r in run(sample_html):
            parsed = date.fromisoformat(r["failure_date"])
            assert parsed.year >= 2000

    def test_no_none_in_required_fields(self, sample_html):
        required = ("cert", "institution_name", "city", "state", "failure_date", "source_url")
        for r in run(sample_html):
            for key in required:
                assert r[key] is not None, f"None in {key}: {r}"
                assert r[key] != "", f"Empty string in {key}: {r}"

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_institution_names_are_strings(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["institution_name"], str)
            assert len(r["institution_name"]) > 0

    def test_states_are_two_letter_codes(self, sample_html):
        for r in run(sample_html):
            assert len(r["state"]) == 2, f"Expected 2-letter state code, got: {r['state']!r}"

    def test_known_entry_silicon_valley_bank(self, sample_html):
        """Silicon Valley Bank (cert 24735) must appear with correct date and state."""
        records = run(sample_html)
        svb = [r for r in records if r["cert"] == 24735]
        assert len(svb) == 1
        assert svb[0]["institution_name"] == "Silicon Valley Bank"
        assert svb[0]["state"] == "CA"
        assert svb[0]["failure_date"] == "2023-03-10"

    def test_known_entry_first_republic(self, sample_html):
        records = run(sample_html)
        frb = [r for r in records if r["cert"] == 59017]
        assert len(frb) == 1
        assert frb[0]["failure_date"] == "2023-05-01"
        assert frb[0]["city"] == "San Francisco"

    def test_all_certs_unique(self, sample_html):
        records = run(sample_html)
        certs = [r["cert"] for r in records]
        assert len(certs) == len(set(certs)), "Duplicate cert numbers in output"

    def test_monetary_fields_absent(self, sample_html):
        """FDIC HTML table has no monetary columns; monetary keys must not appear in record dicts."""
        for r in run(sample_html):
            assert "approx_assets_usd_millions" not in r
            assert "approx_deposits_usd_millions" not in r
            assert "estimated_loss_usd_millions" not in r


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_html_raises_value_error(self):
        with pytest.raises(ValueError):
            run("")

    def test_no_table_raises_value_error(self):
        html = "<html><body><p>No table here.</p></body></html>"
        with pytest.raises(ValueError):
            run(html)

    def test_missing_columns_raises_value_error(self):
        """A table without the expected column headers must raise ValueError."""
        html = """
        <html><body>
        <table id="table">
          <thead><tr><th>Foo</th><th>Bar</th></tr></thead>
          <tbody><tr><td>A</td><td>B</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="Missing expected columns"):
            run(html)

    def test_row_with_bad_cert_is_skipped(self):
        """Rows whose Cert # cell cannot be parsed as an integer are skipped."""
        html = """
        <html><body>
        <table id="table">
          <thead><tr>
            <th>Bank Name</th><th>City</th><th>State</th>
            <th>Cert #</th><th>Acquiring Institution</th>
            <th>Closing Date</th><th>Fund</th>
          </tr></thead>
          <tbody>
            <tr>
              <td>Good Bank</td><td>Austin</td><td>TX</td>
              <td>12345</td><td>Buyer Bank</td>
              <td>06/15/2020</td><td>FDIC-SA</td>
            </tr>
            <tr>
              <td>Bad Cert Bank</td><td>Dallas</td><td>TX</td>
              <td>N/A</td><td>No One</td>
              <td>07/01/2020</td><td>FDIC-SA</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["cert"] == 12345

    def test_row_with_bad_date_is_skipped(self):
        """Rows whose Closing Date is not MM/DD/YYYY are skipped."""
        html = """
        <html><body>
        <table id="table">
          <thead><tr>
            <th>Bank Name</th><th>City</th><th>State</th>
            <th>Cert #</th><th>Acquiring Institution</th>
            <th>Closing Date</th><th>Fund</th>
          </tr></thead>
          <tbody>
            <tr>
              <td>Good Bank</td><td>Austin</td><td>TX</td>
              <td>11111</td><td>Buyer</td>
              <td>06/15/2020</td><td>FDIC-SA</td>
            </tr>
            <tr>
              <td>Bad Date Bank</td><td>Dallas</td><td>TX</td>
              <td>22222</td><td>Buyer</td>
              <td>not-a-date</td><td>FDIC-SA</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["cert"] == 11111

    def test_large_table_all_records_valid(self):
        """50 rows must all parse successfully and produce 50 records."""
        rows = ""
        for i in range(50):
            month = (i % 12) + 1
            day = (i % 28) + 1
            year = 2010 + (i % 14)
            rows += (
                f"<tr>"
                f"<td>Bank {i}</td><td>City {i}</td><td>TX</td>"
                f"<td>{10000 + i}</td><td>Buyer Bank</td>"
                f"<td>{month:02d}/{day:02d}/{year}</td><td>FDIC-SA</td>"
                f"</tr>"
            )
        html = f"""
        <html><body>
        <table id="table">
          <thead><tr>
            <th>Bank Name</th><th>City</th><th>State</th>
            <th>Cert #</th><th>Acquiring Institution</th>
            <th>Closing Date</th><th>Fund</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 50
        for r in records:
            assert date.fromisoformat(r["failure_date"])


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_all_rows_bad_cert_raises_value_error(self):
        """When every data row has an unparseable cert, ValueError is raised."""
        html = """
        <html><body>
        <table id="table">
          <thead><tr>
            <th>Bank Name</th><th>City</th><th>State</th>
            <th>Cert #</th><th>Acquiring Institution</th>
            <th>Closing Date</th><th>Fund</th>
          </tr></thead>
          <tbody>
            <tr>
              <td>Bank A</td><td>City</td><td>TX</td>
              <td>INVALID</td><td>Buyer</td>
              <td>01/01/2020</td><td>FDIC-SA</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No bank failure records"):
            run(html)

    def test_header_only_table_raises_value_error(self):
        """A table with headers but zero valid data rows raises ValueError."""
        html = """
        <html><body>
        <table id="table">
          <thead><tr>
            <th>Bank Name</th><th>City</th><th>State</th>
            <th>Cert #</th><th>Acquiring Institution</th>
            <th>Closing Date</th><th>Fund</th>
          </tr></thead>
          <tbody></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No bank failure records"):
            run(html)


# ---------------------------------------------------------------------------
# Helper-function unit tests
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_standard_format(self):
        assert _parse_date("03/10/2023") == "2023-03-10"

    def test_leading_zeros(self):
        assert _parse_date("01/01/2000") == "2000-01-01"

    def test_december(self):
        assert _parse_date("12/31/2019") == "2019-12-31"

    def test_whitespace_stripped(self):
        assert _parse_date("  05/01/2023  ") == "2023-05-01"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            _parse_date("2023-03-10")

    def test_non_date_raises(self):
        with pytest.raises(ValueError):
            _parse_date("not-a-date")


class TestParseCert:
    def test_plain_integer(self):
        assert _parse_cert("24735") == 24735

    def test_with_commas(self):
        assert _parse_cert("1,234") == 1234

    def test_with_whitespace(self):
        assert _parse_cert("  58317  ") == 58317

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            _parse_cert("N/A")


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record from fixture"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, FdicBankFailureRecord)
        assert msg.cert == records[0]["cert"]
        assert msg.institution_name == records[0]["institution_name"]
        assert msg.city == records[0]["city"]
        assert msg.state == records[0]["state"]
        assert msg.failure_date == records[0]["failure_date"]
        assert msg.source_url == records[0]["source_url"]

    def test_fetch_time_is_iso8601(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert "T" in msg.fetch_time
        assert msg.fetch_time != ""

    def test_monetary_fields_are_none(self, sample_html):
        """Monetary fields must remain None since the HTML has no dollar figures."""
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert msg.approx_assets_usd_millions is None
        assert msg.approx_deposits_usd_millions is None
        assert msg.estimated_loss_usd_millions is None

    def test_cert_is_integer(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert isinstance(msg.cert, int)


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fdic_bank_failures.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.fdic_bank_failures.time.sleep"):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with patch("src.scrapers.fdic_bank_failures.fetch", return_value=fake_resp), \
             patch("src.scrapers.fdic_bank_failures.time.sleep",
                   side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>no tables</p></body></html>"

        with patch("src.scrapers.fdic_bank_failures.fetch", return_value=fake_resp), \
             patch("src.scrapers.fdic_bank_failures.time.sleep"):
            with pytest.raises(ValueError):
                scrape()

    def test_scrape_returns_same_as_run(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fdic_bank_failures.fetch", return_value=fake_resp), \
             patch("src.scrapers.fdic_bank_failures.time.sleep"):
            scraped = scrape()

        direct = run(sample_html)
        assert scraped == direct
