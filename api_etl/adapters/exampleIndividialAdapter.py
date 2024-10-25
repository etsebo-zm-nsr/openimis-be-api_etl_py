from typing import Any

from api_etl.adapters.base import DataAdapter
from api_etl.apps import ApiEtlConfig


class ExampleIndividualAdapter(DataAdapter):
    """
    Adapter allowing data transformation form ExampleIndividualSource to IndividualService input
    """

    def transform(self, data: Any) -> Any:
        """
        Transform paginated result of the ExampleIndividualSource
        Input data is assumed to be Iterable of pages from the example API
        """
        result = []

        for page in data:
            if not "rows" in page:
                raise self.Error("Invalid input, rows field required")

            for row in page["rows"]:
                row.pop("id", None)
                result_row = {"first_name": row.pop(ApiEtlConfig.adapter_first_name_field) or "empty",
                              "last_name": row.pop(ApiEtlConfig.adapter_last_name_field) or "empty",
                              "dob": row.pop(ApiEtlConfig.adapter_dob_field) or "1970-01-01",
                              "json_ext": row}
                result.append(result_row)

        return result
