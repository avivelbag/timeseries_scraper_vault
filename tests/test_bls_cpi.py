"""Tests for src/scrapers/bls_cpi.py.

All tests use a static HTML fixture — no live network calls are made.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.bls_cpi import run, scrape, SOURCE_URL, SERIES_ID, UNITS, REQUIRED_FIELDS
from protos.bls_cpi_pb2 import BLSCpiRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "bls_cpi.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestRunHappyPath:
    def test_returns_records(self, sample_html):
        records = run(sample_html)
        assert len(records) >= 12

    def test_all_records_have_series_id(self, sample_html):
        for record in run(sample_html):
            assert record["series_id"] == SERIES_ID

    def test_all_year_values_are_integers(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["year"], int)
            assert 1900 <= record["year"] <= 2100

    def test_all_month_values_in_range(self, sample_html):
        for record in run(sample_html):
            assert 1 <= record["month"] <= 12

    def test_all_values_are_positive_floats(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["value"], float)
            assert record["value"] > 0

    def test_no_null_year_or_value(self, sample_html):
        """Acceptance criterion: no records with null year/value are emitted."""
        for record in run(sample_html):
            assert record["year"] is not None
            assert record["value"] is not None

    def test_source_url_stored_in_records(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL

    def test_annual_column_not_emitted(self, sample_html):
        """Annual average column must be skipped — month must be 1-12."""
        for record in run(sample_html):
            assert 1 <= record["month"] <= 12

    def test_known_2022_jan_value(self, sample_html):
        records = run(sample_html)
        jan_2022 = [r for r in records if r["year"] == 2022 and r["month"] == 1]
        assert len(jan_2022) == 1
        assert abs(jan_2022[0]["value"] - 281.148) < 0.001

    def test_preliminary_marker_stripped(self, sample_html):
        """Cells like '314.540P' must parse to 314.540 (P suffix stripped)."""
        records = run(sample_html)
        jul_2024 = [r for r in records if r["year"] == 2024 and r["month"] == 7]
        assert len(jul_2024) == 1
        assert abs(jul_2024[0]["value"] - 314.540) < 0.001

    def test_empty_cells_not_emitted(self, sample_html):
        """Empty month cells in 2024 (Sep–Dec) must not produce records."""
        records = run(sample_html)
        sep_2024 = [r for r in records if r["year"] == 2024 and r["month"] == 9]
        assert len(sep_2024) == 0


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_html_without_month_headers_returns_empty_list(self):
        html = "<html><body><table><tr><th>Year</th><th>Q1</th></tr><tr><td>2022</td><td>100.0</td></tr></table></body></html>"
        assert run(html) == []

    def test_html_no_table_returns_empty_list(self):
        html = "<html><body><p>No table here</p></body></html>"
        assert run(html) == []

    def test_malformed_year_row_skipped(self):
        html = """
        <html><body>
        <table>
          <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          <tr><td>notayear</td><td>280.0</td><td>281.0</td></tr>
          <tr><td>2023</td><td>299.0</td><td>300.0</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert all(r["year"] == 2023 for r in records)

    def test_large_table_all_records_valid(self):
        rows = "\n".join(
            f"<tr><td>{year}</td>"
            + "".join(f"<td>{200 + year - 2000 + month * 0.1:.3f}</td>" for month in range(1, 13))
            + "<td>206.0</td></tr>"
            for year in range(2000, 2025)
        )
        html = f"""
        <html><body>
        <table>
          <tr><th>Year</th><th>Jan</th><th>Feb</th><th>Mar</th><th>Apr</th>
              <th>May</th><th>Jun</th><th>Jul</th><th>Aug</th><th>Sep</th>
              <th>Oct</th><th>Nov</th><th>Dec</th><th>Annual</th></tr>
          {rows}
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 25 * 12
        for r in records:
            assert r["year"] is not None
            assert r["value"] is not None

    def test_superscript_footnotes_stripped(self):
        html = """
        <html><body>
        <table>
          <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          <tr><td>2023</td><td>299.1<sup>1</sup></td><td>300.2<sup>2</sup></td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert abs(records[0]["value"] - 299.1) < 0.01
        assert abs(records[1]["value"] - 300.2) < 0.01

    def test_half_columns_skipped(self):
        html = """
        <html><body>
        <table>
          <tr><th>Year</th><th>Jan</th><th>HALF1</th><th>HALF2</th></tr>
          <tr><td>2022</td><td>281.0</td><td>283.0</td><td>295.0</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["month"] == 1


class TestRunRecordStructure:
    def test_required_fields_all_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys in: {record}"

    def test_value_is_float(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["value"], float)

    def test_source_url_matches_module_constant(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL


class TestProtoFieldPopulation:
    def test_proto_fields_populated(self, sample_html):
        """Acceptance criterion: check proto field population."""
        from src.scrapers.bls_cpi import _record_to_proto

        records = run(sample_html)
        assert records, "Need at least one record for proto test"
        msg = _record_to_proto(records[0])

        assert isinstance(msg, BLSCpiRecord)
        assert msg.series_id == SERIES_ID
        assert msg.year == records[0]["year"]
        assert msg.month == records[0]["month"]
        assert abs(msg.value - records[0]["value"]) < 0.001
        assert msg.source_url == SOURCE_URL
        assert msg.units == UNITS
        assert msg.fetch_time is not None

    def test_proto_year_and_month_nonzero(self, sample_html):
        from src.scrapers.bls_cpi import _record_to_proto

        records = run(sample_html)
        for record in records:
            msg = _record_to_proto(record)
            assert msg.year != 0
            assert msg.month != 0

    def test_proto_value_positive(self, sample_html):
        from src.scrapers.bls_cpi import _record_to_proto

        records = run(sample_html)
        for record in records:
            msg = _record_to_proto(record)
            assert msg.value > 0


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body>
        <table>
          <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          <tr><td>2023</td><td>299.0</td><td>300.0</td></tr>
        </table>
        </body></html>
        """
        with patch("src.scrapers.bls_cpi.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.bls_cpi.time.sleep"):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 2

    def test_scrape_sleeps_at_least_3_seconds(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><table><tr><th>Year</th><th>Jan</th></tr></table></body></html>"

        sleep_calls = []
        with patch("src.scrapers.bls_cpi.fetch", return_value=fake_resp), \
             patch("src.scrapers.bls_cpi.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s found; calls: {sleep_calls}"

    def test_scrape_returns_same_as_run(self):
        test_html = """
        <html><body>
        <table>
          <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          <tr><td>2023</td><td>299.0</td><td>300.0</td></tr>
        </table>
        </body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.bls_cpi.fetch", return_value=fake_resp), \
             patch("src.scrapers.bls_cpi.time.sleep"):
            scraped = scrape()

        direct = run(test_html)
        assert scraped == direct
