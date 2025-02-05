import csv
import importlib
import inspect
import io
import sys
from datetime import datetime
from django.core.files.uploadedfile import InMemoryUploadedFile
from typing import Any, Optional


ETL_CLASS = "api_etl.services"
ETL = "api_etl.services.base"


def get_class_by_name(module_name, class_name):
    try:
        # Import the module dynamically
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise ImportError(f"Module '{module_name}' not found.")

    try:
        # Get the class from the module
        class_object = getattr(module, class_name)
    except AttributeError:
        raise ImportError(f"Class '{class_name}' not found in module '{module_name}'.")

    return class_object


def get_classes_in_module(module_name):
    try:
        # Import the module dynamically
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise ImportError(f"Module '{module_name}' not found.")

    # Get all members of the module (classes, functions, variables, etc.)
    members = inspect.getmembers(module, inspect.isclass)

    # Filter to only include classes that are defined in this module (i.e., exclude imported classes)
    classes_in_module = [
        cls_name for cls_name, cls_obj in members
        if module_name in cls_obj.__module__ and 'api_etl.services.base' not in cls_obj.__module__
    ]

    return classes_in_module


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

