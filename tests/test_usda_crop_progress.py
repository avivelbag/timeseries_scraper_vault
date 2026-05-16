"""Tests for src/scrapers/usda_crop_progress.py.

All tests use a static HTML fixture or inline HTML snippets — no live
network calls are made.
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.usda_crop_progress import (
    REQUIRED_FIELDS,
    SOURCE_URL,
    _get_crop_name,
    _is_condition_table,
    _parse_report_week,
    _record_to_proto,
    run,
    scrape,
)
from protos.usda_crop_progress_pb2 import UsdaCropProgressRecord
from bs4 import BeautifulSoup

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "usda_crop_progress_sample.html"
)

_INLINE_HTML = """
<html><body>
<h2>Week Ending May 10, 2026</h2>

<table>
  <caption>CORN Progress</caption>
  <thead>
    <tr><th>State</th><th>Planted</th><th>Emerged</th></tr>
  </thead>
  <tbody>
    <tr><td>Illinois</td><td>52</td><td>28</td></tr>
    <tr><td>Iowa</td><td>48</td><td>22</td></tr>
    <tr><td>Indiana</td><td>45</td><td>--</td></tr>
  </tbody>
</table>

<table>
  <caption>CORN Condition</caption>
  <thead>
    <tr>
      <th>State</th>
      <th>Very Poor</th><th>Poor</th><th>Fair</th><th>Good</th><th>Excellent</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>Illinois</td><td>3</td><td>8</td><td>22</td><td>52</td><td>15</td></tr>
    <tr><td>Iowa</td><td>2</td><td>7</td><td>20</td><td>55</td><td>16</td></tr>
  </tbody>
</table>

<table>
  <caption>SOYBEANS Progress</caption>
  <thead>
    <tr><th>State</th><th>Planted</th><th>Emerged</th></tr>
  </thead>
  <tbody>
    <tr><td>Illinois</td><td>18</td><td>5</td></tr>
    <tr><td>Iowa</td><td>12</td><td>2</td></tr>
    <tr><td>Indiana</td><td>15</td><td>3</td></tr>
  </tbody>
</table>

</body></html>
"""


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestParseReportWeek:
    def test_extracts_date_from_h2(self):
        soup = BeautifulSoup("<h2>Week Ending May 10, 2026</h2>", "lxml")
        assert _parse_report_week(soup) == "2026-05-10"

    def test_extracts_date_without_comma(self):
        soup = BeautifulSoup("<h2>Week Ending May 10 2026</h2>", "lxml")
        assert _parse_report_week(soup) == "2026-05-10"

    def test_case_insensitive(self):
        soup = BeautifulSoup("<h2>WEEK ENDING JANUARY 5, 2025</h2>", "lxml")
        assert _parse_report_week(soup) == "2025-01-05"

    def test_returns_none_when_absent(self):
        soup = BeautifulSoup("<html><body><p>No date here</p></body></html>", "lxml")
        assert _parse_report_week(soup) is None

    def test_fixture_parses_to_correct_date(self, sample_html):
        soup = BeautifulSoup(sample_html, "lxml")
        assert _parse_report_week(soup) == "2026-05-10"


class TestGetCropName:
    def _table(self, html: str):
        return BeautifulSoup(html, "lxml").find("table")

    def test_corn_from_caption(self):
        t = self._table("<table><caption>CORN Progress</caption></table>")
        assert _get_crop_name(t) == "CORN"

    def test_soybeans_from_caption(self):
        t = self._table("<table><caption>SOYBEANS Condition</caption></table>")
        assert _get_crop_name(t) == "SOYBEANS"

    def test_winter_wheat_from_caption(self):
        t = self._table("<table><caption>WINTER WHEAT Progress</caption></table>")
        assert _get_crop_name(t) == "WINTER WHEAT"

    def test_cotton_from_caption(self):
        t = self._table("<table><caption>COTTON Condition</caption></table>")
        assert _get_crop_name(t) == "COTTON"

    def test_returns_none_for_unrecognised_table(self):
        t = self._table("<table><caption>Rice Condition</caption></table>")
        assert _get_crop_name(t) is None

    def test_returns_none_for_table_with_no_caption_or_header(self):
        t = self._table("<table><tr><td>hello</td></tr></table>")
        assert _get_crop_name(t) is None

    def test_crop_from_bold_th_fallback(self):
        html = (
            "<table><tr><th colspan='3'><b>CORN</b></th></tr>"
            "<tr><th>State</th><th>Planted</th></tr></table>"
        )
        t = self._table(html)
        assert _get_crop_name(t) == "CORN"


class TestIsConditionTable:
    def test_condition_columns_detected(self):
        assert _is_condition_table(["Very Poor", "Poor", "Fair", "Good", "Excellent"])

    def test_partial_match_detected(self):
        assert _is_condition_table(["Good", "Excellent"])

    def test_progress_columns_not_detected(self):
        assert not _is_condition_table(["Planted", "Emerged", "Silking"])

    def test_empty_list(self):
        assert not _is_condition_table([])


class TestRunHappyPath:
    def test_fixture_returns_records(self, sample_html):
        assert len(run(sample_html)) > 0

    def test_inline_returns_records(self):
        assert len(run(_INLINE_HTML)) > 0

    def test_fixture_has_at_least_two_crops(self, sample_html):
        crops = {r["crop"] for r in run(sample_html)}
        assert len(crops) >= 2

    def test_fixture_has_at_least_three_states(self, sample_html):
        states = {r["state"] for r in run(sample_html)}
        assert len(states) >= 3

    def test_report_week_is_yyyy_mm_dd(self, sample_html):
        for r in run(sample_html):
            datetime.strptime(r["report_week"], "%Y-%m-%d")

    def test_pct_complete_in_range(self, sample_html):
        for r in run(sample_html):
            assert 0.0 <= r["pct_complete"] <= 100.0

    def test_pct_condition_in_range(self, sample_html):
        for r in run(sample_html):
            assert 0.0 <= r["pct_condition"] <= 100.0

    def test_stage_or_condition_nonempty(self, sample_html):
        for r in run(sample_html):
            assert r["stage"] != "" or r["condition_category"] != ""

    def test_source_url_in_every_record(self, sample_html):
        for r in run(sample_html):
            assert r["source_url"] == SOURCE_URL

    def test_progress_records_have_stage_not_condition(self, sample_html):
        progress = [r for r in run(sample_html) if r["stage"] != ""]
        assert len(progress) > 0
        for r in progress:
            assert r["condition_category"] == ""
            assert r["pct_complete"] > 0.0

    def test_condition_records_have_category_not_stage(self, sample_html):
        condition = [r for r in run(sample_html) if r["condition_category"] != ""]
        assert len(condition) > 0
        for r in condition:
            assert r["stage"] == ""
            assert r["pct_condition"] > 0.0

    def test_specific_corn_planted_illinois(self):
        records = run(_INLINE_HTML)
        rec = next(
            (
                r
                for r in records
                if r["crop"] == "CORN"
                and r["state"] == "Illinois"
                and r["stage"] == "Planted"
            ),
            None,
        )
        assert rec is not None
        assert rec["pct_complete"] == pytest.approx(52.0)

    def test_specific_corn_condition_iowa_good(self):
        records = run(_INLINE_HTML)
        rec = next(
            (
                r
                for r in records
                if r["crop"] == "CORN"
                and r["state"] == "Iowa"
                and r["condition_category"] == "Good"
            ),
            None,
        )
        assert rec is not None
        assert rec["pct_condition"] == pytest.approx(55.0)

    def test_report_week_correct_value(self):
        records = run(_INLINE_HTML)
        assert all(r["report_week"] == "2026-05-10" for r in records)

    def test_fanout_condition_five_records_per_state(self):
        records = run(_INLINE_HTML)
        illinois_condition = [
            r
            for r in records
            if r["crop"] == "CORN"
            and r["state"] == "Illinois"
            and r["condition_category"] != ""
        ]
        assert len(illinois_condition) == 5

    def test_fanout_progress_two_records_per_state_inline(self):
        records = run(_INLINE_HTML)
        illinois_progress = [
            r
            for r in records
            if r["crop"] == "CORN"
            and r["state"] == "Illinois"
            and r["stage"] != ""
        ]
        assert len(illinois_progress) == 2


class TestRequiredFields:
    def test_all_required_fields_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for r in run(sample_html):
            assert required.issubset(r.keys()), f"Missing keys in: {r}"

    def test_field_types(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["report_week"], str)
            assert isinstance(r["state"], str)
            assert isinstance(r["crop"], str)
            assert isinstance(r["stage"], str)
            assert isinstance(r["pct_complete"], float)
            assert isinstance(r["condition_category"], str)
            assert isinstance(r["pct_condition"], float)
            assert isinstance(r["source_url"], str)


class TestEdgeCases:
    def test_empty_html_returns_empty_list(self):
        assert run("") == []

    def test_no_week_heading_returns_empty_list(self):
        html = """
        <html><body>
        <table>
          <caption>CORN Progress</caption>
          <thead><tr><th>State</th><th>Planted</th></tr></thead>
          <tbody><tr><td>Iowa</td><td>50</td></tr></tbody>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_dash_dash_cells_skipped(self):
        html = """
        <html><body>
        <h2>Week Ending May 10, 2026</h2>
        <table>
          <caption>CORN Progress</caption>
          <thead><tr><th>State</th><th>Planted</th><th>Emerged</th></tr></thead>
          <tbody>
            <tr><td>Iowa</td><td>--</td><td>--</td></tr>
            <tr><td>Illinois</td><td>50</td><td>25</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert all(r["state"] == "Illinois" for r in records)

    def test_blank_cells_skipped(self):
        html = """
        <html><body>
        <h2>Week Ending May 10, 2026</h2>
        <table>
          <caption>CORN Condition</caption>
          <thead>
            <tr><th>State</th><th>Very Poor</th><th>Good</th></tr>
          </thead>
          <tbody>
            <tr><td>Iowa</td><td></td><td></td></tr>
            <tr><td>Illinois</td><td>5</td><td>45</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert all(r["state"] == "Illinois" for r in records)

    def test_unrecognised_table_skipped(self):
        html = """
        <html><body>
        <h2>Week Ending May 10, 2026</h2>
        <table>
          <caption>Rice Progress</caption>
          <thead><tr><th>State</th><th>Planted</th></tr></thead>
          <tbody><tr><td>Iowa</td><td>50</td></tr></tbody>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_non_numeric_cells_skipped(self):
        html = """
        <html><body>
        <h2>Week Ending May 10, 2026</h2>
        <table>
          <caption>CORN Progress</caption>
          <thead><tr><th>State</th><th>Planted</th><th>Emerged</th></tr></thead>
          <tbody>
            <tr><td>Iowa</td><td>N/A</td><td>25</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["stage"] == "Emerged"

    def test_large_input_all_valid(self):
        """50 states × 5 condition columns = 250 condition records."""
        states = [f"State{i}" for i in range(50)]
        rows = "".join(
            f"<tr><td>{s}</td><td>10</td><td>15</td><td>30</td><td>35</td><td>10</td></tr>"
            for s in states
        )
        html = f"""
        <html><body>
        <h2>Week Ending May 10, 2026</h2>
        <table>
          <caption>CORN Condition</caption>
          <thead>
            <tr>
              <th>State</th>
              <th>Very Poor</th><th>Poor</th><th>Fair</th><th>Good</th><th>Excellent</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 250
        assert all(0.0 <= r["pct_condition"] <= 100.0 for r in records)

    def test_table_without_state_header_produces_no_records(self):
        html = """
        <html><body>
        <h2>Week Ending May 10, 2026</h2>
        <table>
          <caption>CORN Progress</caption>
          <thead><tr><th>Region</th><th>Planted</th></tr></thead>
          <tbody><tr><td>Midwest</td><td>50</td></tr></tbody>
        </table>
        </body></html>
        """
        assert run(html) == []


class TestProtoFields:
    def test_record_to_proto_progress(self):
        record = {
            "report_week": "2026-05-10",
            "state": "Iowa",
            "crop": "CORN",
            "stage": "Planted",
            "pct_complete": 48.0,
            "condition_category": "",
            "pct_condition": 0.0,
            "source_url": SOURCE_URL,
        }
        msg = _record_to_proto(record)
        assert isinstance(msg, UsdaCropProgressRecord)
        assert msg.report_week == "2026-05-10"
        assert msg.state == "Iowa"
        assert msg.crop == "CORN"
        assert msg.stage == "Planted"
        assert msg.pct_complete == pytest.approx(48.0)
        assert msg.condition_category == ""
        assert msg.pct_condition == pytest.approx(0.0)
        assert msg.source_url == SOURCE_URL
        assert msg.fetch_time != ""
        datetime.fromisoformat(msg.fetch_time)

    def test_record_to_proto_condition(self):
        record = {
            "report_week": "2026-05-10",
            "state": "Iowa",
            "crop": "CORN",
            "stage": "",
            "pct_complete": 0.0,
            "condition_category": "Good",
            "pct_condition": 55.0,
            "source_url": SOURCE_URL,
        }
        msg = _record_to_proto(record)
        assert msg.condition_category == "Good"
        assert msg.pct_condition == pytest.approx(55.0)
        assert msg.stage == ""

    def test_proto_dataclass_defaults(self):
        msg = UsdaCropProgressRecord()
        assert msg.report_week == ""
        assert msg.state == ""
        assert msg.crop == ""
        assert msg.stage == ""
        assert msg.pct_complete == 0.0
        assert msg.condition_category == ""
        assert msg.pct_condition == 0.0
        assert msg.source_url == ""
        assert msg.fetch_time == ""


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch(
            "src.scrapers.usda_crop_progress.fetch", return_value=fake_resp
        ) as mock_fetch:
            records = scrape()
        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_returns_same_as_run(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.usda_crop_progress.fetch", return_value=fake_resp):
            scraped = scrape()
        assert scraped == run(_INLINE_HTML)

    def test_scrape_no_live_network(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch(
            "src.scrapers.usda_crop_progress.fetch", return_value=fake_resp
        ) as mock_fetch:
            scrape()
        assert mock_fetch.call_count == 1
