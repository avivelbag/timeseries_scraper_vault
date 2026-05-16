"""Shared BigQuery streaming-insert module for all scrapers.

Reads project and dataset from BQ_PROJECT and BQ_DATASET environment variables.
"""

import logging
import os

from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

logger = logging.getLogger(__name__)


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


def upload_rows(table_id: str, rows: list[Message], date_column: str = "") -> int:
    """Upload proto messages to a BigQuery table via streaming insert.

    When ``date_column`` is non-empty, calls ``fetch_existing_dates`` before
    inserting and filters out any rows whose date value is already present in
    the table.  The number of skipped rows is logged at INFO level.

    Converts each remaining Message to a dict via MessageToDict (preserving
    field names), then calls insert_rows_json.  Per-row insertion errors are
    logged but do not raise — callers receive the count of rows that succeeded.

    Args:
        table_id: Bare table name without project or dataset prefix.
        rows: Protobuf Message instances to upload.
        date_column: Proto field name (and matching BigQuery column name) used
            for deduplication.  When empty, all rows are uploaded unconditionally.

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

    dicts = [MessageToDict(r, preserving_proto_field_name=True) for r in rows]
    errors = client.insert_rows_json(full_table, dicts)

    for e in errors:
        logger.error("BQ insert error: %s", e)

    return len(dicts) - len(errors)
