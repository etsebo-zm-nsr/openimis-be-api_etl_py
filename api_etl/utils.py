import csv
import io
from datetime import datetime
from django.core.files.uploadedfile import InMemoryUploadedFile
from typing import Any, Optional


def get_timestamped_batch_identifier(prefix='batch_'):
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{prefix}{timestamp}"


def data_to_file(data: list[dict], identifier: Optional[Any] = None) -> InMemoryUploadedFile:
    if not data:
        raise ValueError("The data is empty and cannot be written to a file.")

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=data[0].keys())
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
