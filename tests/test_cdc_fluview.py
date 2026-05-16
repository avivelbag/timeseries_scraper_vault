"""Tests for src/scrapers/cdc_fluview.py.

All tests use a static HTML fixture — no live network calls are made.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.cdc_fluview import (
    run,
    scrape,
    SOURCE_URL,
    REQUIRED_FIELDS,
    _mmwr_week_to_saturday,
    _find_col,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "cdc_fluview_sample.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestMmwrWeekToSaturday:
    def test_week17_2025_ends_april_26(self):
        assert _mmwr_week_to_saturday(2025, 17) == "2025-04-26"

    def test_week16_2025_ends_april_19(self):
        assert _mmwr_week_to_saturday(2025, 16) == "2025-04-19"

    def test_week15_2025_ends_april_12(self):
        assert _mmwr_week_to_saturday(2025, 15) == "2025-04-12"

    def test_week1_2025_ends_january_4(self):
        # MMWR week 1, 2025 ends Jan 4, 2025 (the Saturday containing Jan 4)
        assert _mmwr_week_to_saturday(2025, 1) == "2025-01-04"

    def test_returns_yyyy_mm_dd_format(self):
        import re
        result = _mmwr_week_to_saturday(2025, 10)
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", result)


class TestFindCol:
    def test_finds_exact_keyword(self):
        headers = ["HHS Region", "Year", "MMWR Week", "% Weighted ILI"]
        assert _find_col(headers, "region") == 0

    def test_finds_multi_keyword_match(self):
        headers = ["Total Patients", "ILI Patients", "% Weighted ILI"]
        assert _find_col(headers, "total", "patients") == 0
        assert _find_col(headers, "ili", "patients") == 1

    def test_returns_none_when_no_match(self):
        headers = ["A", "B", "C"]
        assert _find_col(headers, "notpresent") is None

    def test_case_insensitive(self):
        headers = ["HHS REGION", "YEAR"]
        assert _find_col(headers, "region") == 0
        assert _find_col(headers, "year") == 1


class TestRunHappyPath:
    def test_returns_33_records(self, sample_html):
        # 3 weeks × 11 regions (National + 10 HHS) = 33
        assert len(run(sample_html)) == 33

    def test_all_required_fields_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys in: {record}"

    def test_national_region_present_for_each_week(self, sample_html):
        records = run(sample_html)
        national = [r for r in records if r["region"] == "National"]
        assert len(national) == 3

    def test_all_ten_hhs_regions_present_for_week17(self, sample_html):
        records = run(sample_html)
        week17 = [r for r in records if r["week"] == 17]
        region_names = {r["region"] for r in week17}
        expected = {"National"} | {f"Region {i}" for i in range(1, 11)}
        assert expected == region_names

    def test_ili_percent_is_float(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["ili_percent"], float)
            assert record["ili_percent"] > 0

    def test_week_ending_date_format(self, sample_html):
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for record in run(sample_html):
            assert pattern.match(record["week_ending_date"]), (
                f"Bad date format: {record['week_ending_date']}"
            )

    def test_week17_ends_april_26(self, sample_html):
        records = run(sample_html)
        week17 = [r for r in records if r["week"] == 17]
        for r in week17:
            assert r["week_ending_date"] == "2025-04-26"

    def test_week15_ends_april_12(self, sample_html):
        records = run(sample_html)
        week15 = [r for r in records if r["week"] == 15]
        for r in week15:
            assert r["week_ending_date"] == "2025-04-12"

    def test_year_is_int(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["year"], int)
            assert record["year"] == 2025

    def test_week_is_int_in_range(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["week"], int)
            assert 1 <= record["week"] <= 53

    def test_total_patients_positive_int(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["total_patients"], int)
            assert record["total_patients"] > 0

    def test_ili_patients_positive_int(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["ili_patients"], int)
            assert record["ili_patients"] > 0

    def test_ili_patients_less_than_total(self, sample_html):
        for record in run(sample_html):
            assert record["ili_patients"] <= record["total_patients"]

    def test_source_url_points_to_cdc(self, sample_html):
        for record in run(sample_html):
            assert "cdc.gov" in record["source_url"]

    def test_source_url_matches_constant(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_html_without_matching_table_returns_empty_list(self):
        html = "<html><body><table><tr><th>col1</th><th>col2</th></tr></table></body></html>"
        assert run(html) == []

    def test_unknown_region_rows_are_rejected(self):
        html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>Unknown Zone</td><td>2025</td><td>17</td><td>2.0%</td><td>1.9%</td><td>5000</td><td>100</td></tr>
          <tr><td>National</td><td>2025</td><td>17</td><td>1.7%</td><td>1.5%</td><td>50000</td><td>850</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["region"] == "National"

    def test_non_numeric_ili_percent_rows_are_rejected(self):
        html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>2025</td><td>17</td><td>N/A</td><td>1.9%</td><td>50000</td><td>850</td></tr>
          <tr><td>Region 1</td><td>2025</td><td>17</td><td>1.5%</td><td>1.4%</td><td>4000</td><td>60</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["region"] == "Region 1"

    def test_non_numeric_patient_counts_rows_are_rejected(self):
        html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>2025</td><td>17</td><td>1.7%</td><td>1.5%</td><td>--</td><td>850</td></tr>
          <tr><td>Region 2</td><td>2025</td><td>17</td><td>1.5%</td><td>1.3%</td><td>6000</td><td>90</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["region"] == "Region 2"

    def test_non_numeric_year_rows_are_rejected(self):
        html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>N/A</td><td>17</td><td>1.7%</td><td>1.5%</td><td>50000</td><td>850</td></tr>
          <tr><td>Region 3</td><td>2025</td><td>17</td><td>1.8%</td><td>1.6%</td><td>5700</td><td>103</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["region"] == "Region 3"

    def test_rows_with_insufficient_columns_are_skipped(self):
        html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>2025</td></tr>
          <tr><td>Region 4</td><td>2025</td><td>17</td><td>2.0%</td><td>1.8%</td><td>8600</td><td>172</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["region"] == "Region 4"

    def test_table_without_required_headers_is_skipped(self):
        """A table missing required column headers is skipped; the next is used."""
        html = """
        <html><body>
        <table>
          <tr><th>foo</th><th>bar</th></tr>
          <tr><td>x</td><td>y</td></tr>
        </table>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>2025</td><td>17</td><td>1.7%</td><td>1.5%</td><td>50000</td><td>850</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["region"] == "National"

    def test_zero_rows_returns_empty_list(self):
        """A valid table structure with no data rows yields an empty list."""
        html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_ili_percent_without_percent_sign_is_parsed(self):
        """ILI percent cells without trailing % are still parsed correctly."""
        html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>2025</td><td>17</td><td>1.7</td><td>1.5</td><td>50000</td><td>850</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["ili_percent"] == pytest.approx(1.7)


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>2025</td><td>17</td><td>1.7%</td><td>1.5%</td><td>50000</td><td>850</td></tr>
        </table>
        </body></html>
        """
        with patch("src.scrapers.cdc_fluview.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 1
        assert records[0]["region"] == "National"

    def test_scrape_returns_same_as_run(self):
        test_html = """
        <html><body>
        <table>
          <tr>
            <th>HHS Region</th><th>Year</th><th>MMWR Week</th>
            <th>% Weighted ILI</th><th>% Unweighted ILI</th>
            <th>Total Patients</th><th>ILI Patients</th>
          </tr>
          <tr><td>National</td><td>2025</td><td>16</td><td>2.0%</td><td>1.8%</td><td>51200</td><td>1024</td></tr>
          <tr><td>Region 1</td><td>2025</td><td>16</td><td>1.5%</td><td>1.4%</td><td>4050</td><td>61</td></tr>
        </table>
        </body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.cdc_fluview.fetch", return_value=fake_resp):
            scraped = scrape()

        direct = run(test_html)
        assert scraped == direct
