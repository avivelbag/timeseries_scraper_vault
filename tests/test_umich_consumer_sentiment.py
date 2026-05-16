"""Tests for src/scrapers/umich_consumer_sentiment.py."""

import calendar
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from protos.umich_consumer_sentiment_pb2 import (  # type: ignore[attr-defined]
    FINAL,
    PRELIMINARY,
    UmichConsumerSentimentRecord,
)
from src.scrapers.umich_consumer_sentiment import (
    _parse_month,
    _record_to_proto,
    main,
    run,
    scrape,
    scrape_range,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "umich_sca_sample.html"


@pytest.fixture
def fixture_html() -> str:
    return FIXTURE_PATH.read_text()


class TestRunHappyPath:
    def test_record_count(self, fixture_html):
        records = run(fixture_html)
        assert len(records) == 5

    def test_preliminary_flag(self, fixture_html):
        records = run(fixture_html)
        may = next(r for r in records if r["survey_month"] == "2026-05")
        assert may["reading_type"] == PRELIMINARY

    def test_final_reading(self, fixture_html):
        records = run(fixture_html)
        april = next(r for r in records if r["survey_month"] == "2026-04")
        assert april["reading_type"] == FINAL

    def test_survey_month_format(self, fixture_html):
        records = run(fixture_html)
        for r in records:
            assert len(r["survey_month"]) == 7
            assert r["survey_month"][4] == "-"

    def test_float_precision_may(self, fixture_html):
        records = run(fixture_html)
        may = next(r for r in records if r["survey_month"] == "2026-05")
        assert may["index_value"] == pytest.approx(67.4)
        assert may["expectations_index"] == pytest.approx(58.2)
        assert may["current_conditions_index"] == pytest.approx(81.3)

    def test_float_precision_april(self, fixture_html):
        records = run(fixture_html)
        april = next(r for r in records if r["survey_month"] == "2026-04")
        assert april["index_value"] == pytest.approx(52.2)
        assert april["expectations_index"] == pytest.approx(47.3)
        assert april["current_conditions_index"] == pytest.approx(60.4)

    def test_source_url_default(self, fixture_html):
        records = run(fixture_html)
        for r in records:
            assert r["source_url"] == "http://www.sca.isr.umich.edu/"

    def test_source_url_override(self, fixture_html):
        url = "http://example.com/sca"
        records = run(fixture_html, source_url=url)
        for r in records:
            assert r["source_url"] == url

    def test_fetch_time_is_iso(self, fixture_html):
        records = run(fixture_html)
        for r in records:
            assert "T" in r["fetch_time"]

    def test_all_months_present(self, fixture_html):
        records = run(fixture_html)
        months = {r["survey_month"] for r in records}
        assert months == {"2026-01", "2026-02", "2026-03", "2026-04", "2026-05"}


class TestParseMonth:
    def test_preliminary_detection(self):
        month, rt = _parse_month("May 2026 (P)")
        assert month == "2026-05"
        assert rt == PRELIMINARY

    def test_final_detection(self):
        month, rt = _parse_month("April 2026")
        assert month == "2026-04"
        assert rt == FINAL

    def test_january_final(self):
        month, rt = _parse_month("January 2025")
        assert month == "2025-01"
        assert rt == FINAL

    def test_december_preliminary(self):
        month, rt = _parse_month("December 2024 (P)")
        assert month == "2024-12"
        assert rt == PRELIMINARY

    def test_whitespace_stripped(self):
        month, rt = _parse_month("  March 2023  ")
        assert month == "2023-03"
        assert rt == FINAL

    def test_invalid_text_raises(self):
        with pytest.raises(ValueError):
            _parse_month("Not a month")

    def test_bare_number_raises(self):
        with pytest.raises(ValueError):
            _parse_month("2026")


class TestEdgeCases:
    def test_missing_data_row_yields_no_record(self):
        """A row with all '--' values is skipped without raising."""
        html = """
        <html><body><table>
          <tr><th>Month</th>
              <th>Index of Consumer Sentiment</th>
              <th>Index of Consumer Expectations</th>
              <th>Index of Current Economic Conditions</th></tr>
          <tr><td>April 2026</td><td>52.2</td><td>47.3</td><td>60.4</td></tr>
          <tr><td>March 2026</td><td>--</td><td>--</td><td>--</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["survey_month"] == "2026-04"

    def test_partial_missing_data_row_uses_zero(self):
        """A row with some '--' values still produces a record (missing → 0.0)."""
        html = """
        <html><body><table>
          <tr><th>Month</th>
              <th>Index of Consumer Sentiment</th>
              <th>Index of Consumer Expectations</th>
              <th>Index of Current Economic Conditions</th></tr>
          <tr><td>April 2026</td><td>52.2</td><td>--</td><td>60.4</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["expectations_index"] == pytest.approx(0.0)
        assert records[0]["index_value"] == pytest.approx(52.2)

    def test_large_table(self):
        """Table with 108 rows (9 years × 12 months) is parsed in full."""
        rows = []
        for year in range(2017, 2026):
            for month in range(1, 13):
                month_name = calendar.month_name[month]
                rows.append(
                    f"<tr><td>{month_name} {year}</td>"
                    f"<td>{60 + (month % 10):.1f}</td>"
                    f"<td>{55 + (month % 8):.1f}</td>"
                    f"<td>{65 + (month % 12):.1f}</td></tr>"
                )
        html = (
            "<html><body><table>"
            "<tr><th>Month</th>"
            "<th>Index of Consumer Sentiment</th>"
            "<th>Index of Consumer Expectations</th>"
            "<th>Index of Current Economic Conditions</th></tr>"
            + "".join(rows)
            + "</table></body></html>"
        )
        records = run(html)
        assert len(records) == 108

    def test_colspan_header_row(self):
        """Table with a merged colspan title row above the column headers parses correctly."""
        html = """
        <html><body><table>
          <tr><th colspan="4">Summary: Index of Consumer Sentiment</th></tr>
          <tr><th>Month</th>
              <th>Index of Consumer Sentiment</th>
              <th>Index of Consumer Expectations</th>
              <th>Index of Current Economic Conditions</th></tr>
          <tr><td>June 2025</td><td>74.5</td><td>66.3</td><td>87.2</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["survey_month"] == "2025-06"
        assert records[0]["index_value"] == pytest.approx(74.5)
        assert records[0]["reading_type"] == FINAL


class TestFailureModes:
    def test_empty_html_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("")

    def test_whitespace_only_html_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("   \n  ")

    def test_no_table_raises(self):
        with pytest.raises(ValueError, match="No consumer sentiment table"):
            run("<html><body><p>No table here</p></body></html>")

    def test_header_only_table_returns_empty_list(self):
        """Table with only header rows yields [] not an exception."""
        html = """
        <html><body><table>
          <tr><th>Month</th>
              <th>Index of Consumer Sentiment</th>
              <th>Index of Consumer Expectations</th>
              <th>Index of Current Economic Conditions</th></tr>
        </table></body></html>
        """
        records = run(html)
        assert records == []

    def test_non_numeric_values_skipped(self):
        """Rows with non-parseable non-sentinel values yield no record."""
        html = """
        <html><body><table>
          <tr><th>Month</th>
              <th>Index of Consumer Sentiment</th>
              <th>Index of Consumer Expectations</th>
              <th>Index of Current Economic Conditions</th></tr>
          <tr><td>April 2026</td><td>N/A</td><td>N/A</td><td>N/A</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert records == []


class TestRecordToProto:
    def test_proto_field_mapping(self, fixture_html):
        records = run(fixture_html)
        proto = _record_to_proto(records[0])
        assert isinstance(proto, UmichConsumerSentimentRecord)
        assert proto.survey_month == records[0]["survey_month"]
        assert proto.reading_type == records[0]["reading_type"]
        assert proto.index_value == pytest.approx(records[0]["index_value"])
        assert proto.expectations_index == pytest.approx(records[0]["expectations_index"])
        assert proto.current_conditions_index == pytest.approx(
            records[0]["current_conditions_index"]
        )

    def test_proto_fetch_time_set(self, fixture_html):
        records = run(fixture_html)
        proto = _record_to_proto(records[0])
        assert proto.fetch_time != ""
        assert "T" in proto.fetch_time

    def test_proto_preliminary_reading(self, fixture_html):
        records = run(fixture_html)
        may = next(r for r in records if r["survey_month"] == "2026-05")
        proto = _record_to_proto(may)
        assert proto.reading_type == PRELIMINARY


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, fixture_html):
        mock_resp = MagicMock()
        mock_resp.text = fixture_html
        with patch("src.scrapers.umich_consumer_sentiment.fetch", return_value=mock_resp) as mock_fetch:
            with patch("src.scrapers.umich_consumer_sentiment.time.sleep"):
                records = scrape()
        mock_fetch.assert_called_once_with("http://www.sca.isr.umich.edu/")
        assert len(records) == 5

    def test_scrape_sleeps_at_least_3s(self, fixture_html):
        """scrape() must sleep ≥3 s after fetch to satisfy polite-crawl acceptance criterion."""
        mock_resp = MagicMock()
        mock_resp.text = fixture_html
        with patch("src.scrapers.umich_consumer_sentiment.fetch", return_value=mock_resp):
            with patch("src.scrapers.umich_consumer_sentiment.time.sleep") as mock_sleep:
                scrape()
        mock_sleep.assert_called_once()
        sleep_arg = mock_sleep.call_args[0][0]
        assert sleep_arg >= 3, f"Expected sleep ≥3 s, got {sleep_arg}"

    def test_scrape_propagates_parse_error(self):
        mock_resp = MagicMock()
        mock_resp.text = "<html><body><p>No table</p></body></html>"
        with patch("src.scrapers.umich_consumer_sentiment.fetch", return_value=mock_resp):
            with patch("src.scrapers.umich_consumer_sentiment.time.sleep"):
                with pytest.raises(ValueError, match="No consumer sentiment table"):
                    scrape()

    def test_scrape_range_filters_by_year(self, fixture_html):
        mock_resp = MagicMock()
        mock_resp.text = fixture_html
        with patch("src.scrapers.umich_consumer_sentiment.fetch", return_value=mock_resp):
            records = scrape_range(2026, 2026)
        assert len(records) == 5
        assert all(r["survey_month"].startswith("2026") for r in records)

    def test_scrape_range_excludes_out_of_range(self, fixture_html):
        mock_resp = MagicMock()
        mock_resp.text = fixture_html
        with patch("src.scrapers.umich_consumer_sentiment.fetch", return_value=mock_resp):
            records = scrape_range(2020, 2025)
        assert records == []


class TestMain:
    def test_main_calls_upload_rows_without_date_column(self, fixture_html):
        """main() must not pass date_column so preliminary and final for the same
        month are both persisted — the shared uploader cannot express a composite key."""
        mock_resp = MagicMock()
        mock_resp.text = fixture_html
        with patch("src.scrapers.umich_consumer_sentiment.fetch", return_value=mock_resp):
            with patch("src.scrapers.umich_consumer_sentiment.time.sleep"):
                with patch("src.scrapers.umich_consumer_sentiment.upload_rows", return_value=5) as mock_upload:
                    result = main()
        mock_upload.assert_called_once()
        _, kwargs = mock_upload.call_args
        assert "date_column" not in kwargs or kwargs.get("date_column") == "", (
            "main() must not pass date_column to avoid dropping final readings"
        )
        assert result == 5

    def test_main_preserves_both_preliminary_and_final(self, fixture_html):
        """Both preliminary and final records reach upload_rows uncensored.

        The fixture has 5 rows including at least one preliminary (May 2026) and
        several finals. If date_column were set to survey_month, a second scrape
        would filter out the final reading for any month already uploaded.
        This test confirms all 5 rows are passed through.
        """
        mock_resp = MagicMock()
        mock_resp.text = fixture_html
        captured: list = []

        def capture_rows(table, rows, **kw):
            captured.extend(rows)
            return len(rows)

        with patch("src.scrapers.umich_consumer_sentiment.fetch", return_value=mock_resp):
            with patch("src.scrapers.umich_consumer_sentiment.time.sleep"):
                with patch("src.scrapers.umich_consumer_sentiment.upload_rows", side_effect=capture_rows):
                    main()

        reading_types = {r.reading_type for r in captured}
        assert PRELIMINARY in reading_types
        assert len(captured) == 5
