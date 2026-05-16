"""Tests for src/bq_uploader.py.

All tests mock google.cloud.bigquery.Client at the import site in
src.bq_uploader to avoid real network calls and credentials requirements.
"""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from google.api_core.exceptions import NotFound

from src.bq_uploader import fetch_existing_dates, upload_rows
from protos.eia_petroleum_prices_pb2 import PetroleumPriceRecord  # type: ignore[attr-defined]


def _make_proto_row(region: str = "U.S.", price: float = 3.12) -> PetroleumPriceRecord:
    msg = PetroleumPriceRecord()
    msg.source_url = "https://www.eia.gov/test"
    msg.period_date = "2025-01-06"
    msg.product = "petroleum"
    msg.region = region
    msg.price_usd_per_gallon = price
    msg.grade = "Regular"
    msg.units = "USD/gallon"
    return msg


class TestUploadRowsHappyPath:
    def test_calls_insert_rows_json_with_correct_full_table(self):
        """Verifies the fully-qualified table id (project.dataset.table) is passed."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []  # no errors

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch.dict(os.environ, {"BQ_PROJECT": "my-project", "BQ_DATASET": "my_dataset"}):
            upload_rows("eia_petroleum_prices", [_make_proto_row()])

        mock_client.insert_rows_json.assert_called_once()
        table_arg = mock_client.insert_rows_json.call_args[0][0]
        assert table_arg == "my-project.my_dataset.eia_petroleum_prices"

    def test_client_is_created_with_correct_project(self):
        """Verifies bigquery.Client receives the BQ_PROJECT value."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client) as mock_cls, \
             patch.dict(os.environ, {"BQ_PROJECT": "proj-x", "BQ_DATASET": "ds"}):
            upload_rows("tbl", [_make_proto_row()])

        mock_cls.assert_called_once_with(project="proj-x")

    def test_rows_are_converted_from_proto_to_dict_before_insert(self):
        """Verifies MessageToDict is called on each row and the dicts reach insert_rows_json."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []
        fake_dict = {"source_url": "https://eia.gov", "region": "U.S.", "price_usd_per_gallon": 3.12}

        proto_row = _make_proto_row()

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch("src.bq_uploader.MessageToDict", return_value=fake_dict) as mock_to_dict, \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}):
            upload_rows("tbl", [proto_row])

        mock_to_dict.assert_called_once_with(proto_row, preserving_proto_field_name=True)
        inserted_dicts = mock_client.insert_rows_json.call_args[0][1]
        assert inserted_dicts == [fake_dict]

    def test_returns_row_count_when_no_errors(self):
        """Returns len(rows) when insert_rows_json reports no errors."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}):
            result = upload_rows("tbl", [_make_proto_row("U.S."), _make_proto_row("East Coast")])

        assert result == 2

    def test_multiple_rows_all_converted_and_inserted(self):
        """Verifies MessageToDict is called once per row for a batch."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        rows = [_make_proto_row("U.S.", 3.0), _make_proto_row("Midwest", 2.9), _make_proto_row("West Coast", 3.5)]

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch("src.bq_uploader.MessageToDict", side_effect=lambda r, **kw: {"region": r.region}) as mock_to_dict, \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}):
            result = upload_rows("tbl", rows)

        assert mock_to_dict.call_count == 3
        assert result == 3


class TestUploadRowsEdgeCases:
    def test_empty_rows_returns_zero(self):
        """Empty input must return 0 without calling insert_rows_json."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}):
            result = upload_rows("tbl", [])

        assert result == 0

    def test_large_batch_converts_all_rows(self):
        """All 100 rows must be converted and the return value must be 100."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        rows = [_make_proto_row("Region", float(i)) for i in range(100)]

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch("src.bq_uploader.MessageToDict", side_effect=lambda r, **kw: {}) as mock_to_dict, \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}):
            result = upload_rows("tbl", rows)

        assert mock_to_dict.call_count == 100
        assert result == 100

    def test_table_id_does_not_include_project_or_dataset(self):
        """Ensures bare table_id is not double-prefixed in the final table ref."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch.dict(os.environ, {"BQ_PROJECT": "proj", "BQ_DATASET": "ds"}):
            upload_rows("just_table", [_make_proto_row()])

        table_arg = mock_client.insert_rows_json.call_args[0][0]
        assert table_arg == "proj.ds.just_table"
        assert table_arg.count(".") == 2


class TestUploadRowsErrorHandling:
    def test_insertion_errors_are_logged_and_do_not_raise(self, caplog):
        """Per-row BQ errors must be logged at ERROR level and not propagate."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = [
            {"index": 0, "errors": [{"reason": "invalid", "message": "bad row"}]}
        ]

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}), \
             caplog.at_level(logging.ERROR, logger="src.bq_uploader"):
            result = upload_rows("tbl", [_make_proto_row()])

        assert len(caplog.records) == 1
        assert "BQ insert error" in caplog.records[0].message
        assert result == 0

    def test_partial_errors_reduce_return_count(self, caplog):
        """When some rows error, return value equals total minus error count."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = [
            {"index": 1, "errors": [{"reason": "invalid"}]}
        ]

        rows = [_make_proto_row("A"), _make_proto_row("B"), _make_proto_row("C")]

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}), \
             caplog.at_level(logging.ERROR, logger="src.bq_uploader"):
            result = upload_rows("tbl", rows)

        assert result == 2
        assert len(caplog.records) == 1

    def test_multiple_errors_all_logged(self, caplog):
        """Every error entry from insert_rows_json must produce one log record."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = [
            {"index": 0, "errors": [{"reason": "invalid"}]},
            {"index": 2, "errors": [{"reason": "quota"}]},
        ]

        rows = [_make_proto_row() for _ in range(3)]

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}), \
             caplog.at_level(logging.ERROR, logger="src.bq_uploader"):
            result = upload_rows("tbl", rows)

        assert len(caplog.records) == 2
        assert result == 1

    def test_missing_bq_project_raises_key_error(self):
        """KeyError is raised when BQ_PROJECT environment variable is absent."""
        env = {"BQ_DATASET": "ds"}
        # Ensure BQ_PROJECT is not set
        with patch("src.bq_uploader.bigquery.Client", return_value=MagicMock()), \
             patch.dict(os.environ, env, clear=False):
            os.environ.pop("BQ_PROJECT", None)
            with pytest.raises(KeyError):
                upload_rows("tbl", [_make_proto_row()])

    def test_missing_bq_dataset_raises_key_error(self):
        """KeyError is raised when BQ_DATASET environment variable is absent."""
        with patch("src.bq_uploader.bigquery.Client", return_value=MagicMock()), \
             patch.dict(os.environ, {"BQ_PROJECT": "proj"}, clear=False):
            os.environ.pop("BQ_DATASET", None)
            with pytest.raises(KeyError):
                upload_rows("tbl", [_make_proto_row()])


def _make_proto_row_with_date(period_date: str, region: str = "U.S.", price: float = 3.12) -> PetroleumPriceRecord:
    msg = PetroleumPriceRecord()
    msg.source_url = "https://www.eia.gov/test"
    msg.period_date = period_date
    msg.product = "petroleum"
    msg.region = region
    msg.price_usd_per_gallon = price
    msg.grade = "Regular"
    msg.units = "USD/gallon"
    return msg


class TestFetchExistingDates:
    def test_returns_set_of_date_strings_from_bq(self):
        """Happy path: query results are returned as a set of strings."""
        mock_client = MagicMock()
        mock_client.query.return_value.result.return_value = [
            {"period_date": "2025-01-01"},
            {"period_date": "2025-01-02"},
        ]

        result = fetch_existing_dates(mock_client, "proj.ds.tbl", "period_date")

        assert result == {"2025-01-01", "2025-01-02"}
        mock_client.query.assert_called_once()
        assert "period_date" in mock_client.query.call_args[0][0]

    def test_returns_empty_set_when_table_not_found(self):
        """NotFound exception from BQ is caught and an empty set is returned."""
        mock_client = MagicMock()
        mock_client.query.side_effect = NotFound("table not found")

        result = fetch_existing_dates(mock_client, "proj.ds.new_table", "period_date")

        assert result == set()

    def test_empty_table_returns_empty_set(self):
        """A table with no rows produces an empty set without error."""
        mock_client = MagicMock()
        mock_client.query.return_value.result.return_value = []

        result = fetch_existing_dates(mock_client, "proj.ds.tbl", "period_date")

        assert result == set()

    def test_query_includes_limit_and_date_column(self):
        """The issued query must include DISTINCT, the column name, and LIMIT 50000."""
        mock_client = MagicMock()
        mock_client.query.return_value.result.return_value = []

        fetch_existing_dates(mock_client, "proj.ds.tbl", "my_date_col")

        issued_query: str = mock_client.query.call_args[0][0]
        assert "DISTINCT" in issued_query
        assert "my_date_col" in issued_query
        assert "50000" in issued_query


class TestUploadRowsDeduplication:
    def test_dedup_skips_existing_date_and_uploads_new(self, caplog):
        """Rows whose date already exists in BQ are filtered; only new rows are inserted.

        Mocks fetch_existing_dates to return {"2025-01-01"}.  A two-row batch with
        period_dates "2025-01-01" and "2025-01-02" must result in exactly 1 row
        inserted (the "2025-01-02" row) and 1 row skipped.
        """
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        row_existing = _make_proto_row_with_date("2025-01-01")
        row_new = _make_proto_row_with_date("2025-01-02")

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch("src.bq_uploader.fetch_existing_dates", return_value={"2025-01-01"}), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}), \
             caplog.at_level(logging.INFO, logger="src.bq_uploader"):
            result = upload_rows("tbl", [row_existing, row_new], date_column="period_date")

        inserted_dicts = mock_client.insert_rows_json.call_args[0][1]
        assert result == 1
        assert len(inserted_dicts) == 1
        assert inserted_dicts[0]["period_date"] == "2025-01-02"
        assert "Skipped 1" in caplog.text

    def test_dedup_uploads_all_when_no_existing_dates(self):
        """When fetch_existing_dates returns empty set, all rows are uploaded."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        rows = [_make_proto_row_with_date("2025-01-01"), _make_proto_row_with_date("2025-01-02")]

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch("src.bq_uploader.fetch_existing_dates", return_value=set()), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}):
            result = upload_rows("tbl", rows, date_column="period_date")

        assert result == 2
        assert len(mock_client.insert_rows_json.call_args[0][1]) == 2

    def test_dedup_skips_all_when_all_dates_exist(self, caplog):
        """When all row dates are already in BQ, no insert is made and 0 is returned."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        rows = [_make_proto_row_with_date("2025-01-01"), _make_proto_row_with_date("2025-01-02")]

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch("src.bq_uploader.fetch_existing_dates", return_value={"2025-01-01", "2025-01-02"}), \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}), \
             caplog.at_level(logging.INFO, logger="src.bq_uploader"):
            result = upload_rows("tbl", rows, date_column="period_date")

        inserted_dicts = mock_client.insert_rows_json.call_args[0][1]
        assert result == 0
        assert len(inserted_dicts) == 0
        assert "Skipped 2" in caplog.text

    def test_no_date_column_skips_dedup_entirely(self):
        """Without date_column, fetch_existing_dates is never called."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        with patch("src.bq_uploader.bigquery.Client", return_value=mock_client), \
             patch("src.bq_uploader.fetch_existing_dates") as mock_fetch, \
             patch.dict(os.environ, {"BQ_PROJECT": "p", "BQ_DATASET": "d"}):
            upload_rows("tbl", [_make_proto_row_with_date("2025-01-01")])

        mock_fetch.assert_not_called()
