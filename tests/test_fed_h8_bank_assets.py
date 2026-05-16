"""Tests for src/scrapers/fed_h8_bank_assets.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import re
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.fed_h8_bank_assets import (
    SOURCE_URL,
    _matches_target,
    _parse_column_date,
    _record_to_proto,
    _seasonal_adjustment_label,
    main,
    run,
    scrape,
)
from protos.fed_h8_bank_assets_pb2 import FedH8BankAssets

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fed_h8_bank_assets.html")

_REQUIRED_SERIES = [
    "Total loans and leases",
    "Commercial and industrial loans",
    "Real estate loans",
    "Consumer loans",
    "Total deposits",
    "Securities",
]


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_six_sa_series_present(self, sample_html):
        """Fixture SA table must yield exactly the six required series."""
        sa_records = [r for r in run(sample_html) if r["seasonal_adjustment"] == "SA"]
        sa_labels = {r["series_label"] for r in sa_records}
        assert sa_labels == set(_REQUIRED_SERIES)

    def test_six_nsa_series_present(self, sample_html):
        """Fixture NSA table must yield exactly the six required series."""
        nsa_records = [r for r in run(sample_html) if r["seasonal_adjustment"] == "NSA"]
        nsa_labels = {r["series_label"] for r in nsa_records}
        assert nsa_labels == set(_REQUIRED_SERIES)

    def test_total_records_twelve(self, sample_html):
        """6 series × 2 tables (SA + NSA) = 12 records total."""
        assert len(run(sample_html)) == 12

    def test_value_millions_usd_positive_float(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["value_millions_usd"], float)
            assert r["value_millions_usd"] > 0

    def test_week_ending_is_thursday(self, sample_html):
        """The H.8 reference day is Thursday (weekday == 3)."""
        for r in run(sample_html):
            d = datetime.fromisoformat(r["week_ending"])
            assert d.weekday() == 3, (
                f"Expected Thursday but got weekday {d.weekday()} for {r['week_ending']}"
            )

    def test_week_ending_is_iso8601(self, sample_html):
        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for r in run(sample_html):
            assert iso_pattern.match(r["week_ending"]), f"Bad date: {r['week_ending']}"

    def test_most_recent_date_used(self, sample_html):
        """Fixture has three date columns; the rightmost (May 1, 2025) must be used."""
        for r in run(sample_html):
            assert r["week_ending"] == "2025-05-01"

    def test_seasonal_adjustment_values(self, sample_html):
        adjustments = {r["seasonal_adjustment"] for r in run(sample_html)}
        assert adjustments == {"SA", "NSA"}

    def test_units_field_is_millions_usd(self, sample_html):
        for r in run(sample_html):
            assert r["units"] == "millions_usd"

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_all_required_fields_present(self, sample_html):
        required_keys = {
            "week_ending", "series_label", "value_millions_usd",
            "seasonal_adjustment", "source_url", "units",
        }
        for r in run(sample_html):
            assert required_keys.issubset(r.keys())

    def test_known_value_total_loans_sa(self, sample_html):
        sa_total_loans = [
            r for r in run(sample_html)
            if r["series_label"] == "Total loans and leases" and r["seasonal_adjustment"] == "SA"
        ]
        assert len(sa_total_loans) == 1
        assert sa_total_loans[0]["value_millions_usd"] == pytest.approx(12_140_000.0)

    def test_known_value_total_deposits_nsa(self, sample_html):
        nsa_deposits = [
            r for r in run(sample_html)
            if r["series_label"] == "Total deposits" and r["seasonal_adjustment"] == "NSA"
        ]
        assert len(nsa_deposits) == 1
        assert nsa_deposits[0]["value_millions_usd"] == pytest.approx(17_875_000.0)


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_html_raises_value_error(self):
        with pytest.raises(ValueError, match="No H.8 bank asset records"):
            run("")

    def test_no_table_raises_value_error(self):
        with pytest.raises(ValueError):
            run("<html><body><p>No table here</p></body></html>")

    def test_table_without_date_columns_raises_value_error(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>Account</th><th>Not a date</th></tr></thead>
          <tbody><tr><td>Total loans and leases</td><td>12000.0</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_non_target_rows_not_emitted(self):
        """Rows like 'Other liabilities' that don't match any target are silently ignored."""
        html = """
        <html><body>
        <table>
          <caption>Seasonally adjusted, millions of dollars</caption>
          <thead><tr><th>Account</th><th>May 1, 2025</th></tr></thead>
          <tbody>
            <tr><td>Other liabilities</td><td>500000.0</td></tr>
            <tr><td>Total deposits</td><td>17900000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert all(r["series_label"] != "Other liabilities" for r in records)
        assert len(records) == 1
        assert records[0]["series_label"] == "Total deposits"

    def test_comma_separated_values_parsed(self):
        """Values like '12,140,000.0' must strip commas and parse to a float."""
        html = """
        <html><body>
        <table>
          <caption>Seasonally adjusted</caption>
          <thead><tr><th>Account</th><th>May 1, 2025</th></tr></thead>
          <tbody>
            <tr><td>Securities</td><td>5,840,000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["value_millions_usd"] == pytest.approx(5_840_000.0)

    def test_large_table_all_target_series_extracted(self):
        """Table with many extra rows still extracts exactly the 6 target series."""
        extra_rows = "".join(
            f"<tr><td>Extra series {i}</td><td>{1000.0 * i}</td></tr>"
            for i in range(1, 11)
        )
        target_rows = "".join(
            f"<tr><td>{s}</td><td>{float(idx + 1) * 1_000_000}</td></tr>"
            for idx, s in enumerate(_REQUIRED_SERIES)
        )
        html = f"""
        <html><body>
        <table>
          <caption>Seasonally adjusted</caption>
          <thead><tr><th>Account</th><th>May 1, 2025</th></tr></thead>
          <tbody>{extra_rows}{target_rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        labels = {r["series_label"] for r in records}
        assert labels == set(_REQUIRED_SERIES)

    def test_abbreviated_month_with_period_parsed(self):
        """Column header 'Apr. 24, 2025' (with period) must parse to 2025-04-24."""
        html = """
        <html><body>
        <table>
          <caption>Seasonally adjusted</caption>
          <thead><tr><th>Account</th><th>Apr. 24, 2025</th></tr></thead>
          <tbody>
            <tr><td>Total deposits</td><td>17850000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert records[0]["week_ending"] == "2025-04-24"

    def test_double_space_in_date_header_parsed(self):
        """'May  1, 2025' (double space before single digit day) must parse correctly."""
        html = """
        <html><body>
        <table>
          <caption>Seasonally adjusted</caption>
          <thead><tr><th>Account</th><th>May  1, 2025</th></tr></thead>
          <tbody>
            <tr><td>Total deposits</td><td>17900000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert records[0]["week_ending"] == "2025-05-01"


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_asterisk_row_not_emitted(self):
        """Row with asterisk label must not produce any record."""
        html = """
        <html><body>
        <table>
          <caption>Seasonally adjusted</caption>
          <thead><tr><th>Account</th><th>May 1, 2025</th></tr></thead>
          <tbody>
            <tr><td>Total deposits</td><td>17900000.0</td></tr>
            <tr><td>*</td><td colspan="1">Note</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        labels = [r["series_label"] for r in records]
        assert "*" not in labels
        assert len(records) == 1

    def test_unparseable_value_row_skipped(self):
        """Row with 'n/a' in the value cell must be skipped; other rows kept."""
        html = """
        <html><body>
        <table>
          <caption>Seasonally adjusted</caption>
          <thead><tr><th>Account</th><th>May 1, 2025</th></tr></thead>
          <tbody>
            <tr><td>Total loans and leases</td><td>n/a</td></tr>
            <tr><td>Total deposits</td><td>17900000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_label"] == "Total deposits"

    def test_table_without_thead_skipped(self):
        """Tables lacking a <thead> must not contribute any records."""
        html = """
        <html><body>
        <table>
          <tbody>
            <tr><td>Total deposits</td><td>17900000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_securities_prefix_match_not_other_securities(self):
        """'Other securities' must not match the 'Securities' target series."""
        html = """
        <html><body>
        <table>
          <caption>Seasonally adjusted</caption>
          <thead><tr><th>Account</th><th>May 1, 2025</th></tr></thead>
          <tbody>
            <tr><td>Other securities</td><td>1000000.0</td></tr>
            <tr><td>Securities</td><td>5840000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["series_label"] == "Securities"

    def test_nsa_label_from_caption(self):
        """Table with 'Not seasonally adjusted' in caption must emit 'NSA' records."""
        html = """
        <html><body>
        <table>
          <caption>Not seasonally adjusted, millions of dollars</caption>
          <thead><tr><th>Account</th><th>May 1, 2025</th></tr></thead>
          <tbody>
            <tr><td>Total deposits</td><td>17875000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert records[0]["seasonal_adjustment"] == "NSA"


# ---------------------------------------------------------------------------
# _parse_column_date unit tests
# ---------------------------------------------------------------------------


class TestParseColumnDate:
    def test_abbreviated_month_no_period(self):
        assert _parse_column_date("Apr 24, 2025") == "2025-04-24"

    def test_abbreviated_month_with_period(self):
        assert _parse_column_date("Apr. 24, 2025") == "2025-04-24"

    def test_full_month_name(self):
        assert _parse_column_date("April 24, 2025") == "2025-04-24"

    def test_double_space_before_single_digit_day(self):
        assert _parse_column_date("May  1, 2025") == "2025-05-01"

    def test_no_date_returns_none(self):
        assert _parse_column_date("Account") is None

    def test_blank_returns_none(self):
        assert _parse_column_date("") is None

    def test_january_single_digit(self):
        assert _parse_column_date("Jan. 2, 2025") == "2025-01-02"

    def test_december(self):
        assert _parse_column_date("December 25, 2025") == "2025-12-25"


# ---------------------------------------------------------------------------
# _matches_target unit tests
# ---------------------------------------------------------------------------


class TestMatchesTarget:
    def test_exact_match(self):
        assert _matches_target("Total deposits") == "Total deposits"

    def test_case_insensitive(self):
        assert _matches_target("total deposits") == "Total deposits"

    def test_prefix_match(self):
        assert _matches_target("Total loans and leases, net") == "Total loans and leases"

    def test_other_securities_not_matched(self):
        assert _matches_target("Other securities") is None

    def test_unknown_series_returns_none(self):
        assert _matches_target("Total assets") is None

    def test_blank_returns_none(self):
        assert _matches_target("") is None

    def test_all_six_targets_match(self):
        for target in _REQUIRED_SERIES:
            assert _matches_target(target) == target

    def test_consumer_loans_with_suffix(self):
        assert _matches_target("Consumer loans and credit cards") == "Consumer loans"


# ---------------------------------------------------------------------------
# _seasonal_adjustment_label unit tests
# ---------------------------------------------------------------------------


class TestSeasonalAdjustmentLabel:
    def test_caption_sa(self):
        html = """
        <table>
          <caption>Seasonally adjusted, millions of dollars</caption>
          <thead><tr><th>Account</th></tr></thead>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _seasonal_adjustment_label(table) == "SA"

    def test_caption_nsa(self):
        html = """
        <table>
          <caption>Not seasonally adjusted, millions of dollars</caption>
          <thead><tr><th>Account</th></tr></thead>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _seasonal_adjustment_label(table) == "NSA"

    def test_preceding_heading_sa(self):
        html = """
        <div>
          <h2>Seasonally Adjusted, All Commercial Banks</h2>
          <table><thead><tr><th>Account</th></tr></thead></table>
        </div>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _seasonal_adjustment_label(table) == "SA"

    def test_preceding_heading_nsa(self):
        html = """
        <div>
          <h2>Not Seasonally Adjusted, All Commercial Banks</h2>
          <table><thead><tr><th>Account</th></tr></thead></table>
        </div>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        assert _seasonal_adjustment_label(table) == "NSA"

    def test_default_sa_when_no_label(self):
        html = """<table><thead><tr><th>Account</th></tr></thead></table>"""
        table = BeautifulSoup(html, "lxml").find("table")
        assert _seasonal_adjustment_label(table) == "SA"


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record from fixture"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, FedH8BankAssets)
        assert msg.week_ending == records[0]["week_ending"]
        assert msg.series_label == records[0]["series_label"]
        assert msg.value_millions_usd == pytest.approx(records[0]["value_millions_usd"])
        assert msg.seasonal_adjustment == records[0]["seasonal_adjustment"]
        assert msg.source_url == records[0]["source_url"]
        assert msg.units == "millions_usd"
        assert msg.fetch_time != ""

    def test_fetch_time_is_iso8601(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        assert iso_pattern.match(msg.fetch_time), f"Bad fetch_time: {msg.fetch_time}"

    def test_units_field_set_correctly(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert msg.units == "millions_usd"


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fed_h8_bank_assets.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.fed_h8_bank_assets.time.sleep"):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with patch("src.scrapers.fed_h8_bank_assets.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_h8_bank_assets.time.sleep",
                   side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body>no tables</body></html>"

        with patch("src.scrapers.fed_h8_bank_assets.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_h8_bank_assets.time.sleep"):
            with pytest.raises(ValueError):
                scrape()

    def test_scrape_returns_same_as_run(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fed_h8_bank_assets.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_h8_bank_assets.time.sleep"):
            scraped = scrape()

        direct = run(sample_html)
        assert scraped == direct


# ---------------------------------------------------------------------------
# BigQuery upload integration tests
# ---------------------------------------------------------------------------


class TestMainBigQueryUpload:
    def test_main_calls_upload_rows_with_correct_table(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fed_h8_bank_assets.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_h8_bank_assets.time.sleep"), \
             patch("src.scrapers.fed_h8_bank_assets.upload_rows", return_value=12) as mock_upload:
            result = main()

        mock_upload.assert_called_once()
        table_arg = mock_upload.call_args[0][0]
        assert table_arg == "fed_h8_bank_assets"
        assert result == 12

    def test_main_passes_proto_messages_to_upload(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.fed_h8_bank_assets.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_h8_bank_assets.time.sleep"), \
             patch("src.scrapers.fed_h8_bank_assets.upload_rows", return_value=12) as mock_upload:
            main()

        messages = mock_upload.call_args[0][1]
        assert len(messages) == 12
        assert all(isinstance(m, FedH8BankAssets) for m in messages)
