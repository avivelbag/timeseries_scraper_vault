"""Tests for src/scrapers/bea_gdp.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.bea_gdp import (
    SOURCE_URL,
    _build_col_info,
    _clean_value,
    _period_to_date,
    _record_to_proto,
    run,
    scrape,
)
from protos.bea_gdp_pb2 import GdpRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "bea_gdp_table.html")


@pytest.fixture
def fixture_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def pct_table_html() -> str:
    """Minimal HTML containing only a percent-change table."""
    return """<html><body>
    <h2>Percent Change From Preceding Period</h2>
    <table>
      <caption>Seasonally adjusted at annual rates; percent change from preceding period</caption>
      <thead>
        <tr>
          <th rowspan="2">Line</th>
          <th rowspan="2">Component</th>
          <th colspan="2">2024</th>
        </tr>
        <tr><th>I</th><th>II</th></tr>
      </thead>
      <tbody>
        <tr><th>1</th><td>Gross domestic product</td><td>1.4</td><td>3.0</td></tr>
        <tr><th>2</th><td>Personal consumption expenditures</td><td>1.9</td><td>2.9</td></tr>
      </tbody>
    </table>
    </body></html>"""


class TestColumnHeaderDetection:
    def test_two_row_header_produces_correct_labels(self, fixture_html):
        soup = BeautifulSoup(fixture_html, "lxml")
        tables = soup.find_all("table")
        assert tables, "fixture must contain at least one table"
        thead = tables[0].find("thead")
        n_skip, labels = _build_col_info(thead)
        assert n_skip == 2, "Line + Component columns must be skipped"
        expected = [
            "2023Q1", "2023Q2", "2023Q3", "2023Q4",
            "2024Q1", "2024Q2", "2024Q3", "2024Q4",
        ]
        assert labels == expected

    def test_single_row_header_returns_empty_labels(self):
        html = """<table>
          <thead>
            <tr><th>Component</th><th>2024Q1</th><th>2024Q2</th></tr>
          </thead>
          <tbody></tbody>
        </table>"""
        soup = BeautifulSoup(html, "lxml")
        thead = soup.find("thead")
        _, labels = _build_col_info(thead)
        assert labels == []

    def test_no_thead_returns_zero_skip_and_empty(self):
        html = "<table><tbody><tr><td>x</td></tr></tbody></table>"
        soup = BeautifulSoup(html, "lxml")
        thead = soup.find("thead")
        n_skip, labels = _build_col_info(thead)
        assert n_skip == 0
        assert labels == []

    def test_colspan_expansion_populates_year_for_each_quarter(self):
        html = """<table>
          <thead>
            <tr>
              <th rowspan="2">Component</th>
              <th colspan="4">2022</th>
            </tr>
            <tr><th>I</th><th>II</th><th>III</th><th>IV</th></tr>
          </thead>
          <tbody></tbody>
        </table>"""
        soup = BeautifulSoup(html, "lxml")
        thead = soup.find("thead")
        n_skip, labels = _build_col_info(thead)
        assert n_skip == 1
        assert labels == ["2022Q1", "2022Q2", "2022Q3", "2022Q4"]

    def test_unrecognised_quarter_label_produces_none(self):
        html = """<table>
          <thead>
            <tr>
              <th rowspan="2">Component</th>
              <th colspan="2">2024</th>
            </tr>
            <tr><th>I</th><th>Annual</th></tr>
          </thead>
          <tbody></tbody>
        </table>"""
        soup = BeautifulSoup(html, "lxml")
        thead = soup.find("thead")
        _, labels = _build_col_info(thead)
        assert labels[0] == "2024Q1"
        assert labels[1] is None


class TestMultiRowParse:
    def test_returns_records_for_known_components(self, fixture_html):
        records = run(fixture_html)
        components = {r["component"] for r in records}
        assert "Gross domestic product" in components
        assert "Personal consumption expenditures" in components

    def test_indented_component_names_stripped(self, fixture_html):
        records = run(fixture_html)
        components = {r["component"] for r in records}
        # &#160; prefix stripped; component name should not start with space
        assert "Goods" in components
        assert "Services" in components
        for c in components:
            assert not c.startswith(" "), f"component has leading space: {c!r}"

    def test_all_records_have_required_fields(self, fixture_html):
        records = run(fixture_html)
        for r in records:
            assert "period_date" in r
            assert "component" in r
            assert "value_billions_usd" in r
            assert "pct_change_annualized" in r
            assert "source_url" in r

    def test_period_date_format_is_iso_date(self, fixture_html):
        import re
        records = run(fixture_html)
        for r in records:
            assert re.match(r"^\d{4}-\d{2}-01$", r["period_date"]), (
                f"unexpected period_date: {r['period_date']}"
            )

    def test_source_url_propagated_to_all_records(self, fixture_html):
        records = run(fixture_html, source_url="https://example.com/gdp")
        for r in records:
            assert r["source_url"] == "https://example.com/gdp"

    def test_level_values_are_positive_for_gdp(self, fixture_html):
        records = run(fixture_html)
        gdp_records = [r for r in records if r["component"] == "Gross domestic product"]
        assert gdp_records, "Gross domestic product must appear in records"
        for r in gdp_records:
            assert r["value_billions_usd"] > 0

    def test_net_exports_level_is_negative(self, fixture_html):
        records = run(fixture_html)
        net_exp = [r for r in records if r["component"] == "Net exports of goods and services"]
        assert net_exp, "Net exports must appear in records"
        for r in net_exp:
            assert r["value_billions_usd"] < 0

    def test_minimum_record_count_from_fixture(self, fixture_html):
        records = run(fixture_html)
        assert len(records) >= 20, (
            f"expected at least 20 records from fixture, got {len(records)}"
        )

    def test_pct_change_populated_from_pct_table(self, fixture_html):
        records = run(fixture_html)
        gdp_q1_2024 = [
            r for r in records
            if r["component"] == "Gross domestic product" and r["period_date"] == "2024-01-01"
        ]
        assert len(gdp_q1_2024) == 1
        assert abs(gdp_q1_2024[0]["pct_change_annualized"] - 1.4) < 0.001

    def test_level_value_parsed_correctly_for_gdp_2023q1(self, fixture_html):
        records = run(fixture_html)
        gdp_2023q1 = [
            r for r in records
            if r["component"] == "Gross domestic product" and r["period_date"] == "2023-01-01"
        ]
        assert len(gdp_2023q1) == 1
        assert abs(gdp_2023q1[0]["value_billions_usd"] - 22068.2) < 0.01

    def test_empty_html_raises_value_error(self):
        with pytest.raises(ValueError, match="No GDP records"):
            run("")

    def test_html_with_no_table_raises_value_error(self):
        with pytest.raises(ValueError, match="No GDP records"):
            run("<html><body><p>Nothing here.</p></body></html>")


class TestNumericCleaning:
    def test_plain_float(self):
        assert _clean_value("3.5") == pytest.approx(3.5)

    def test_comma_thousands_separator(self):
        assert _clean_value("22,068.2") == pytest.approx(22068.2)

    def test_parentheses_for_negative(self):
        assert _clean_value("(800.1)") == pytest.approx(-800.1)

    def test_parentheses_with_comma(self):
        assert _clean_value("(1,234.5)") == pytest.approx(-1234.5)

    def test_empty_string_returns_none(self):
        assert _clean_value("") is None

    def test_whitespace_only_returns_none(self):
        assert _clean_value("   ") is None

    def test_ellipsis_returns_none(self):
        assert _clean_value("...") is None

    def test_not_applicable_returns_none(self):
        assert _clean_value("N/A") is None

    def test_non_numeric_text_returns_none(self):
        assert _clean_value("n/a") is None

    def test_negative_pct_from_fixture(self, fixture_html):
        records = run(fixture_html)
        goods_q2_2023 = [
            r for r in records
            if r["component"] == "Goods" and r["period_date"] == "2023-04-01"
        ]
        assert len(goods_q2_2023) == 1
        assert goods_q2_2023[0]["pct_change_annualized"] == pytest.approx(-1.9)


class TestRejectionOfRowsWithMissingValues:
    def test_row_with_empty_component_is_skipped(self, fixture_html):
        records = run(fixture_html)
        for r in records:
            assert r["component"] != "", "empty component must be skipped"

    def test_row_with_all_empty_cells_produces_no_records(self):
        html = """<html><body>
        <h2>Percent Change</h2>
        <table>
          <caption>percent change from preceding period</caption>
          <thead>
            <tr><th rowspan="2">Line</th><th rowspan="2">Component</th><th colspan="2">2024</th></tr>
            <tr><th>I</th><th>II</th></tr>
          </thead>
          <tbody>
            <tr><th>1</th><td>Gross domestic product</td><td>1.4</td><td>3.0</td></tr>
            <tr><th>2</th><td>Net exports of goods and services</td><td></td><td></td></tr>
          </tbody>
        </table>
        </body></html>"""
        records = run(html)
        components = {r["component"] for r in records}
        assert "Net exports of goods and services" not in components, (
            "row with all-empty cells must not produce records"
        )
        assert "Gross domestic product" in components

    def test_partial_missing_values_still_emit_record(self):
        """A row with some empty cells still emits records for the non-empty periods."""
        html = """<html><body>
        <h2>Percent Change</h2>
        <table>
          <caption>percent change from preceding period</caption>
          <thead>
            <tr><th rowspan="2">Component</th><th colspan="2">2024</th></tr>
            <tr><th>I</th><th>II</th></tr>
          </thead>
          <tbody>
            <tr><td>Gross domestic product</td><td>1.4</td><td></td></tr>
          </tbody>
        </table>
        </body></html>"""
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_date"] == "2024-01-01"

    def test_nonparseable_value_cell_skipped(self):
        html = """<html><body>
        <h2>Level</h2>
        <table>
          <caption>billions of dollars</caption>
          <thead>
            <tr><th rowspan="2">Component</th><th colspan="2">2024</th></tr>
            <tr><th>I</th><th>II</th></tr>
          </thead>
          <tbody>
            <tr><td>GDP</td><td>not-a-number</td><td>25000.0</td></tr>
          </tbody>
        </table>
        </body></html>"""
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_date"] == "2024-04-01"
        assert records[0]["value_billions_usd"] == pytest.approx(25000.0)


class TestPeriodToDate:
    def test_q1_maps_to_january(self):
        assert _period_to_date("2024Q1") == "2024-01-01"

    def test_q2_maps_to_april(self):
        assert _period_to_date("2024Q2") == "2024-04-01"

    def test_q3_maps_to_july(self):
        assert _period_to_date("2023Q3") == "2023-07-01"

    def test_q4_maps_to_october(self):
        assert _period_to_date("2022Q4") == "2022-10-01"

    def test_invalid_input_returns_none(self):
        assert _period_to_date("2024-Q1") is None
        assert _period_to_date("") is None
        assert _period_to_date("2024") is None


class TestRecordToProto:
    def test_all_fields_populated(self, fixture_html):
        records = run(fixture_html)
        assert records
        msg = _record_to_proto(records[0])
        assert isinstance(msg, GdpRecord)
        assert msg.period_date != ""
        assert msg.component != ""
        assert msg.source_url == SOURCE_URL
        assert msg.schema_version == 1

    def test_fetch_time_is_set(self, fixture_html):
        records = run(fixture_html)
        msg = _record_to_proto(records[0])
        assert msg.fetch_time._dt is not None

    def test_value_and_pct_types_are_float(self, fixture_html):
        records = run(fixture_html)
        for r in records:
            msg = _record_to_proto(r)
            assert isinstance(msg.value_billions_usd, float)
            assert isinstance(msg.pct_change_annualized, float)

    def test_schema_version_is_one(self, fixture_html):
        records = run(fixture_html)
        for r in records:
            msg = _record_to_proto(r)
            assert msg.schema_version == 1


class TestScrapeFunction:
    def _make_simple_html(self) -> str:
        return """<html><body>
        <h2>Percent Change</h2>
        <table>
          <caption>percent change from preceding period</caption>
          <thead>
            <tr><th rowspan="2">Component</th><th colspan="1">2024</th></tr>
            <tr><th>I</th></tr>
          </thead>
          <tbody>
            <tr><td>Gross domestic product</td><td>1.4</td></tr>
          </tbody>
        </table>
        </body></html>"""

    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = self._make_simple_html()

        with patch("src.scrapers.bea_gdp.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.bea_gdp.time.sleep"):
            scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)

    def test_scrape_sleeps_at_least_3s(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = self._make_simple_html()

        sleep_calls: list[float] = []
        with patch("src.scrapers.bea_gdp.fetch", return_value=fake_resp), \
             patch("src.scrapers.bea_gdp.time.sleep", side_effect=sleep_calls.append):
            scrape()

        assert any(s >= 3 for s in sleep_calls), (
            f"expected at least one sleep ≥3 s, got {sleep_calls}"
        )

    def test_scrape_returns_list_of_dicts(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = self._make_simple_html()

        with patch("src.scrapers.bea_gdp.fetch", return_value=fake_resp), \
             patch("src.scrapers.bea_gdp.time.sleep"):
            result = scrape()

        assert isinstance(result, list)
        assert len(result) >= 1
        assert "component" in result[0]
