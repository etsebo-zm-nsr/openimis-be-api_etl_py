import csv
import io
from datetime import datetime
from django.core.files.uploadedfile import InMemoryUploadedFile
from typing import Any, Optional


def get_timestamped_batch_identifier(prefix='batch_'):
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{prefix}{timestamp}"


def union_of_keys(data: list[dict]) -> list[str]:
    """Every key any row carries, in first-seen order.

    Adapters legitimately emit a column only for the rows it applies to - a linkage
    note is attached to the flagged record, not to the 99% that matched cleanly. Taking
    the header from the first row alone turns that into a hard `ValueError: dict
    contains fields not in fieldnames` the moment a flagged row is not first in the
    batch, which is a data-dependent failure: the same code succeeds or fails according
    to row order. The union makes the header a property of the batch, and `restval`
    writes the rows that lack the column as blank rather than dropping them.
    """
    return list({key: None for row in data for key in row})


def data_to_file(data: list[dict], identifier: Optional[Any] = None) -> InMemoryUploadedFile:
    if not data:
        raise ValueError("The data is empty and cannot be written to a file.")

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=union_of_keys(data), restval="")
    writer.writeheader()
    writer.writerows(data)

    filename = f"{identifier or 'data'}.csv"
    buffer.seek(0)

    return InMemoryUploadedFile(
        file=io.BytesIO(buffer.getvalue().encode("utf-8")),
        field_name="file",
        name=filename,
        content_type="text/csv",
        size=buffer.tell(),
        charset="utf-8"
    )
