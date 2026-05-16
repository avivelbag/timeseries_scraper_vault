"""Tests for src/scrapers/bls_ppi.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.bls_ppi import (
    LANDING_URL,
    REQUIRED_FIELDS,
    _find_table_links,
    _parse_value,
    run,
    scrape,
)
from protos.bls_ppi_pb2 import BLSPpiRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "bls_ppi_table.html")

EXPECTED_SERIES_ID = "WPS00000000"
EXPECTED_DESCRIPTION = "All commodities"


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestParseValue:
    def test_plain_float_parsed(self):
        val, prelim = _parse_value("230.1")
        assert val is not None
        assert abs(val - 230.1) < 0.001
        assert not prelim

    def test_preliminary_marker_stripped_and_flagged(self):
        val, prelim = _parse_value("222.8(P)")
        assert val is not None
        assert abs(val - 222.8) < 0.001
        assert prelim

    def test_preliminary_lowercase_flagged(self):
        val, prelim = _parse_value("100.5(p)")
        assert val is not None
        assert prelim

    def test_em_dash_returns_none(self):
        val, prelim = _parse_value("—")
        assert val is None
        assert not prelim

    def test_empty_string_returns_none(self):
        val, prelim = _parse_value("")
        assert val is None
        assert not prelim

    def test_whitespace_only_returns_none(self):
        val, prelim = _parse_value("   ")
        assert val is None

    def test_non_numeric_text_returns_none(self):
        val, prelim = _parse_value("N/A")
        assert val is None


class TestRunHappyPath:
    def test_returns_records(self, sample_html):
        records = run(sample_html)
        assert len(records) >= 12

    def test_fixture_yields_32_records(self, sample_html):
        """2022×12 + 2023×12 + 2024×8 (Jul/Aug are (P), Sep-Dec missing/dash)."""
        records = run(sample_html)
        assert len(records) == 32

    def test_series_id_extracted_correctly(self, sample_html):
        records = run(sample_html)
        for r in records:
            assert r["series_id"] == EXPECTED_SERIES_ID

    def test_commodity_description_extracted(self, sample_html):
        records = run(sample_html)
        for r in records:
            assert EXPECTED_DESCRIPTION in r["commodity_description"]

    def test_period_format_yyyy_mm(self, sample_html):
        period_re = re.compile(r"^\d{4}-\d{2}$")
        for r in run(sample_html):
            assert period_re.match(r["period"]), f"Bad period: {r['period']}"

    def test_all_index_values_positive_float(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["index_value"], float)
            assert r["index_value"] > 0

    def test_first_three_records_values(self, sample_html):
        """Acceptance criterion: first records match known fixture values."""
        records = run(sample_html)
        assert records[0]["period"] == "2022-01"
        assert abs(records[0]["index_value"] - 230.1) < 0.01
        assert not records[0]["preliminary"]

        assert records[1]["period"] == "2022-02"
        assert abs(records[1]["index_value"] - 232.5) < 0.01
        assert not records[1]["preliminary"]

        assert records[2]["period"] == "2022-03"
        assert abs(records[2]["index_value"] - 239.8) < 0.01
        assert not records[2]["preliminary"]

    def test_preliminary_flag_set_on_p_cells(self, sample_html):
        """Acceptance criterion: (P) suffix sets preliminary=True."""
        records = run(sample_html)
        jul24 = [r for r in records if r["period"] == "2024-07"]
        assert len(jul24) == 1
        assert jul24[0]["preliminary"]
        assert abs(jul24[0]["index_value"] - 222.8) < 0.01

        aug24 = [r for r in records if r["period"] == "2024-08"]
        assert len(aug24) == 1
        assert aug24[0]["preliminary"]
        assert abs(aug24[0]["index_value"] - 223.5) < 0.01

    def test_non_preliminary_records_have_flag_false(self, sample_html):
        records = run(sample_html)
        non_prelim = [r for r in records if r["period"].startswith("2022")]
        assert all(not r["preliminary"] for r in non_prelim)

    def test_annual_column_not_emitted(self, sample_html):
        records = run(sample_html)
        for r in records:
            month = int(r["period"].split("-")[1])
            assert 1 <= month <= 12

    def test_missing_dash_cells_not_emitted(self, sample_html):
        """Acceptance criterion: non-numeric cells produce no records."""
        records = run(sample_html)
        sep24 = [r for r in records if r["period"] == "2024-09"]
        oct24 = [r for r in records if r["period"] == "2024-10"]
        assert len(sep24) == 0
        assert len(oct24) == 0

    def test_empty_cells_not_emitted(self, sample_html):
        records = run(sample_html)
        nov24 = [r for r in records if r["period"] == "2024-11"]
        dec24 = [r for r in records if r["period"] == "2024-12"]
        assert len(nov24) == 0
        assert len(dec24) == 0

    def test_source_url_in_all_records(self, sample_html):
        records = run(sample_html, source_url="http://example.com/ppi")
        for r in records:
            assert r["source_url"] == "http://example.com/ppi"


class TestRunPercentChanges:
    def test_percent_change_1m_zero_for_first_record(self, sample_html):
        """No prior month in fixture for 2022-01, so change must be 0.0."""
        records = run(sample_html)
        assert records[0]["period"] == "2022-01"
        assert records[0]["percent_change_1m"] == 0.0

    def test_percent_change_12m_zero_for_first_year(self, sample_html):
        """No 2021 data in fixture, so 12-month changes for 2022 must be 0.0."""
        records = run(sample_html)
        year_2022 = [r for r in records if r["period"].startswith("2022")]
        for r in year_2022:
            assert r["percent_change_12m"] == 0.0

    def test_percent_change_1m_computed_correctly(self, sample_html):
        """2022-02 = 232.5, 2022-01 = 230.1 → change ≈ 1.043%."""
        records = run(sample_html)
        feb22 = [r for r in records if r["period"] == "2022-02"][0]
        expected = (232.5 - 230.1) / 230.1 * 100
        assert abs(feb22["percent_change_1m"] - expected) < 0.01

    def test_percent_change_12m_computed_correctly(self, sample_html):
        """2023-01 = 228.9, 2022-01 = 230.1 → change ≈ -0.521%."""
        records = run(sample_html)
        jan23 = [r for r in records if r["period"] == "2023-01"][0]
        expected = (228.9 - 230.1) / 230.1 * 100
        assert abs(jan23["percent_change_12m"] - expected) < 0.01

    def test_percent_change_1m_cross_year_boundary(self, sample_html):
        """2023-01 prior month is 2022-12 = 231.2."""
        records = run(sample_html)
        jan23 = [r for r in records if r["period"] == "2023-01"][0]
        expected = (228.9 - 231.2) / 231.2 * 100
        assert abs(jan23["percent_change_1m"] - expected) < 0.01


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_no_table_returns_empty_list(self):
        assert run("<html><body><p>No data here</p></body></html>") == []

    def test_table_without_month_headers_returns_empty_list(self):
        html = (
            "<html><body><table>"
            "<tr><th>Year</th><th>Q1</th><th>Q2</th></tr>"
            "<tr><td>2023</td><td>100.0</td><td>101.0</td></tr>"
            "</table></body></html>"
        )
        assert run(html) == []

    def test_malformed_year_rows_skipped(self):
        """Acceptance criterion: rows with non-numeric year cells are rejected."""
        html = """
        <html><body>
        <p>Series Id: WPS99999999<br>Item: Test series</p>
        <table>
          <thead>
            <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          </thead>
          <tbody>
            <tr><td>notayear</td><td>100.0</td><td>101.0</td></tr>
            <tr><td>2023</td><td>102.0</td><td>103.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert all(r["period"].startswith("2023") for r in records)

    def test_large_table_all_records_valid(self):
        rows = "\n".join(
            f"<tr><td>{year}</td>"
            + "".join(
                f"<td>{200 + (year - 2000) + month * 0.1:.1f}</td>"
                for month in range(1, 13)
            )
            + "<td>206.0</td></tr>"
            for year in range(2000, 2025)
        )
        html = f"""
        <html><body>
        <p>Series Id: WPS00000000<br>Item: All commodities<br>Base Period: 1982=100</p>
        <table>
          <thead>
            <tr><th colspan="14">WPS00000000 All commodities</th></tr>
            <tr>
              <th>Year</th><th>Jan</th><th>Feb</th><th>Mar</th><th>Apr</th>
              <th>May</th><th>Jun</th><th>Jul</th><th>Aug</th><th>Sep</th>
              <th>Oct</th><th>Nov</th><th>Dec</th><th>Annual</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 25 * 12
        for r in records:
            assert isinstance(r["index_value"], float)
            assert r["index_value"] > 0

    def test_all_non_numeric_dash_cells_rejected(self):
        """Acceptance criterion: rows with only non-numeric values emit nothing."""
        html = """
        <html><body>
        <p>Series Id: WPS12345678<br>Item: Dash test<br>Base Period: 1982=100</p>
        <table>
          <thead>
            <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          </thead>
          <tbody>
            <tr><td>2024</td><td>—</td><td>—</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert records == []

    def test_mixed_dash_and_valid_cells(self):
        html = """
        <html><body>
        <p>Series Id: WPS12345678<br>Item: Mixed test<br>Base Period: 1982=100</p>
        <table>
          <thead>
            <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          </thead>
          <tbody>
            <tr><td>2024</td><td>—</td><td>105.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["period"] == "2024-02"
        assert abs(records[0]["index_value"] - 105.0) < 0.01


class TestRunRecordStructure:
    def test_all_required_fields_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys: {record}"

    def test_no_internal_year_month_keys_leaked(self, sample_html):
        """_year and _month are internal helpers and must not appear in output."""
        for record in run(sample_html):
            assert "_year" not in record
            assert "_month" not in record

    def test_index_value_is_float(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["index_value"], float)

    def test_preliminary_is_bool(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["preliminary"], bool)


class TestProtoFieldPopulation:
    def test_proto_fields_populated(self, sample_html):
        """Acceptance criterion: BLSPpiRecord fields match the source record."""
        from src.scrapers.bls_ppi import _record_to_proto

        records = run(sample_html)
        assert records, "Need at least one record"
        msg = _record_to_proto(records[0])

        assert isinstance(msg, BLSPpiRecord)
        assert msg.series_id == EXPECTED_SERIES_ID
        assert msg.period == records[0]["period"]
        assert abs(msg.index_value - records[0]["index_value"]) < 0.001
        assert msg.preliminary == records[0]["preliminary"]
        assert msg.source_url == records[0]["source_url"]
        assert msg.fetch_time is not None

    def test_proto_index_value_positive(self, sample_html):
        from src.scrapers.bls_ppi import _record_to_proto

        for record in run(sample_html):
            msg = _record_to_proto(record)
            assert msg.index_value > 0

    def test_proto_percent_changes_are_floats(self, sample_html):
        from src.scrapers.bls_ppi import _record_to_proto

        records = run(sample_html)
        msg = _record_to_proto(records[1])
        assert isinstance(msg.percent_change_1m, float)
        assert isinstance(msg.percent_change_12m, float)


class TestFindTableLinks:
    def test_finds_all_commodities_link(self):
        html = """
        <html><body>
        <a href="/ppi/tables/history/wps00000000.htm">All Commodities</a>
        <a href="/ppi/tables/history/wpsfd49116.htm">Final Demand</a>
        </body></html>
        """
        links = _find_table_links(html, "https://www.bls.gov/")
        assert len(links) == 2
        assert any("wps00000000" in lnk for lnk in links)
        assert any("wpsfd49116" in lnk for lnk in links)

    def test_deduplicates_links(self):
        html = """
        <html><body>
        <a href="/ppi/tables/history/wps00000000.htm">All Commodities</a>
        <a href="/ppi/tables/history/wps00000000.htm">All Commodities (link 2)</a>
        </body></html>
        """
        links = _find_table_links(html, "https://www.bls.gov/")
        assert len(links) == 1

    def test_ignores_irrelevant_links(self):
        html = """
        <html><body>
        <a href="/cpi/">Consumer Price Index</a>
        <a href="/ppi/">PPI Home</a>
        </body></html>
        """
        links = _find_table_links(html, "https://www.bls.gov/")
        assert links == []

    def test_resolves_relative_urls(self):
        html = '<html><body><a href="/ppi/history/wps.htm">All Commodities</a></body></html>'
        links = _find_table_links(html, "https://www.bls.gov/ppi/tables.htm")
        assert links[0].startswith("https://www.bls.gov")


class TestScrapeFunction:
    def test_scrape_fetches_landing_url(self):
        landing_html = """
        <html><body>
        <a href="/ppi/tables/history/wps00000000.htm">All Commodities</a>
        </body></html>
        """
        table_html = """
        <html><body>
        <p>Series Id: WPS00000000<br>Item: All commodities<br>Base Period: 1982=100</p>
        <table>
          <thead>
            <tr><th>Year</th><th>Jan</th><th>Feb</th></tr>
          </thead>
          <tbody>
            <tr><td>2023</td><td>228.9</td><td>230.1</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        landing_resp = MagicMock(spec=requests.Response)
        landing_resp.text = landing_html
        table_resp = MagicMock(spec=requests.Response)
        table_resp.text = table_html

        with (
            patch("src.scrapers.bls_ppi.fetch", side_effect=[landing_resp, table_resp]) as mock_fetch,
            patch("src.scrapers.bls_ppi.time.sleep"),
        ):
            records = scrape()

        assert mock_fetch.call_args_list[0][0][0] == LANDING_URL
        assert len(records) == 2

    def test_scrape_sleeps_at_least_3_seconds_per_request(self):
        landing_resp = MagicMock(spec=requests.Response)
        landing_resp.text = "<html><body></body></html>"

        sleep_calls: list[float] = []
        with (
            patch("src.scrapers.bls_ppi.fetch", return_value=landing_resp),
            patch("src.scrapers.bls_ppi.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_returns_empty_when_no_links_found(self):
        resp = MagicMock(spec=requests.Response)
        resp.text = "<html><body><p>No links here</p></body></html>"

        with (
            patch("src.scrapers.bls_ppi.fetch", return_value=resp),
            patch("src.scrapers.bls_ppi.time.sleep"),
        ):
            records = scrape()

        assert records == []
