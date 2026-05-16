"""Shared BigQuery streaming-insert module for all scrapers.

Reads project and dataset from BQ_PROJECT and BQ_DATASET environment variables.
"""

import dataclasses
import logging
import os
from typing import Any

from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

logger = logging.getLogger(__name__)


def _record_to_dict(r: Any) -> dict:
    """Serialize a record to a JSON-compatible dict for BigQuery insertion.

    Dispatches on the record type:
    - Real protobuf Message instances use MessageToDict (preserves field names).
    - Dataclass stubs use dataclasses.asdict with _Timestamp conversion to ISO
      strings so BigQuery's insert_rows_json receives plain JSON-serializable
      values.  The _Timestamp stub stores the datetime in its ``._dt``
      attribute; a missing or None ._dt falls back to an empty string.

    Args:
        r: A protobuf Message instance or a dataclass stub instance.

    Returns:
        Dict suitable for passing to bigquery.Client.insert_rows_json.

    Raises:
        TypeError: When r is neither a Message nor a dataclass instance.
    """
    if isinstance(r, Message):
        return MessageToDict(r, preserving_proto_field_name=True)
    if dataclasses.is_dataclass(r) and not isinstance(r, type):
        result: dict = {}
        for k, v in dataclasses.asdict(r).items():
            if hasattr(v, "_dt"):
                # _Timestamp stub: convert datetime to ISO string.
                dt = v._dt
                result[k] = dt.isoformat() if dt is not None else ""
            else:
                result[k] = v
        return result
    raise TypeError(f"Cannot serialize record of type {type(r)!r}")


def fetch_existing_dates(client: bigquery.Client, table_id: str, date_column: str) -> set[str]:
    """Return the set of distinct date strings already present in a BigQuery table.

    Runs ``SELECT DISTINCT {date_column} FROM {table_id} LIMIT 50000`` and
    returns each value as an ISO-8601 string.  Returns an empty set when the
    table does not yet exist so callers can run against a fresh dataset without
    error.

    # TODO: LIMIT 50000 will silently truncate if the table grows beyond that count.

    Args:
        client: An authenticated BigQuery client.
        table_id: Fully-qualified table reference (project.dataset.table).
        date_column: Column name to query for existing date values.

    Returns:
        Set of date strings already present in the table, or an empty set if
        the table does not exist.
    """
    query = f"SELECT DISTINCT {date_column} FROM `{table_id}` LIMIT 50000"
    try:
        rows = client.query(query).result()
        return {str(row[date_column]) for row in rows}
    except NotFound:
        return set()


def upload_rows(table_id: str, rows: list[Any], date_column: str = "") -> int:
    """Upload records to a BigQuery table via streaming insert.

    When ``date_column`` is non-empty, calls ``fetch_existing_dates`` before
    inserting and filters out any rows whose date value is already present in
    the table.  The number of skipped rows is logged at INFO level.

    Serializes each record via ``_record_to_dict``, which supports both real
    protobuf Message instances (via MessageToDict) and hand-written dataclass
    stubs (via dataclasses.asdict with _Timestamp conversion).  Per-row
    insertion errors are logged but do not raise — callers receive the count
    of rows that succeeded.

    Args:
        table_id: Bare table name without project or dataset prefix.
        rows: Record instances to upload — protobuf Messages or dataclass stubs.
        date_column: Field name used for deduplication.  When empty, all rows
            are uploaded unconditionally.

    Returns:
        Count of rows successfully inserted (total minus error-row count).

    Raises:
        KeyError: If BQ_PROJECT or BQ_DATASET environment variables are not set.
    """
    project = os.environ["BQ_PROJECT"]
    dataset = os.environ["BQ_DATASET"]
    full_table = f"{project}.{dataset}.{table_id}"

    client = bigquery.Client(project=project)

    if date_column:
        existing = fetch_existing_dates(client, full_table, date_column)
        before = len(rows)
        rows = [r for r in rows if getattr(r, date_column) not in existing]
        skipped = before - len(rows)
        if skipped:
            logger.info("Skipped %d rows already present in %s", skipped, full_table)

    dicts = [_record_to_dict(r) for r in rows]
    errors = client.insert_rows_json(full_table, dicts)

    for e in errors:
        logger.error("BQ insert error: %s", e)

    return len(dicts) - len(errors)
