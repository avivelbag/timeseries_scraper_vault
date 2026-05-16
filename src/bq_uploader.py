"""Shared BigQuery streaming-insert module for all scrapers.

Reads project and dataset from BQ_PROJECT and BQ_DATASET environment variables.
"""

import logging
import os

from google.cloud import bigquery
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

logger = logging.getLogger(__name__)


def upload_rows(table_id: str, rows: list[Message]) -> int:
    """Upload proto messages to a BigQuery table via streaming insert.

    Converts each Message to a dict via MessageToDict (preserving field names),
    then calls insert_rows_json. Per-row insertion errors are logged but do not
    raise — callers receive the count of rows that succeeded.

    Args:
        table_id: Bare table name without project or dataset prefix.
        rows: Protobuf Message instances to upload.

    Returns:
        Count of rows successfully inserted (total minus error-row count).

    Raises:
        KeyError: If BQ_PROJECT or BQ_DATASET environment variables are not set.
    """
    project = os.environ["BQ_PROJECT"]
    dataset = os.environ["BQ_DATASET"]
    full_table = f"{project}.{dataset}.{table_id}"

    client = bigquery.Client(project=project)
    dicts = [MessageToDict(r, preserving_proto_field_name=True) for r in rows]
    errors = client.insert_rows_json(full_table, dicts)

    for e in errors:
        logger.error("BQ insert error: %s", e)

    return len(dicts) - len(errors)
