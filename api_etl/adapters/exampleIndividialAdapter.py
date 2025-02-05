from typing import Any, Iterable

from api_etl.adapters.base import DataAdapter
from api_etl.apps import ApiEtlConfig


class ExampleIndividualAdapter(DataAdapter):
    """
    Adapter allowing data transformation form ExampleIndividualSource to IndividualService input
    """

    def transform(self, data: Iterable[Any]) -> Iterable[Any]:
        """
        Transform paginated result of the ExampleIndividualSource
        Input data is assumed to be Iterable of pages from the example API
        """
        result = []

        if data is None:
            raise self.Error(f"Invalid input, expect input not to be None")

        for row in data:
            row.pop("id", None)
            result_row = {"first_name": row.pop(ApiEtlConfig.adapter_first_name_field) or "empty",
                          "last_name": row.pop(ApiEtlConfig.adapter_last_name_field) or "empty",
                          "dob": row.pop(ApiEtlConfig.adapter_dob_field) or "1970-01-01",
                          "location_name": row.get(ApiEtlConfig.adapter_location_name_field),
                          "location_code": row.get(ApiEtlConfig.adapter_location_code_field),}
            result.append(result_row)

        return result
