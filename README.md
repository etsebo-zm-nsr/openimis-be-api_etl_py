# openIMIS Backend api_etl reference module

# API ETL Configuration and Adapter Extension Guide

## Default Configuration Example

The following is a sample configuration (`DEFAULT_CONFIG`) that enables connection to an example API. This configuration is used to control authentication, data source connection, and field mappings for an adapter:

```python
DEFAULT_CONFIG = {
    "auth_type": "basic",  # Options: noauth, basic, bearer
    "auth_basic_username": "<USERNAME>",  # Basic auth username
    "auth_basic_password": "<PASSWORD>",  # Basic auth password
    "auth_bearer_token": "",  # Bearer token for 'bearer' auth_type

    "source_http_method": "post",  # HTTP method for requests.request
    "source_url": "http://41.175.18.170:8070/api/mobile/v1/beneficiary/active/search",
    "source_headers": {
        "Content-Type": "application/x-www-form-urlencodedc",
        "Accept": "application/json"
    },
    "source_batch_size": 50,  # Number of records to fetch in one batch

    "adapter_first_name_field": "firstName",
    "adapter_last_name_field": "lastName",
    "adapter_dob_field": "dateOfBirth",

    "skip_integration_test": False  # Must be False for production deployments
}
```

## Creating a New Adapter

To implement a new adapter, you must:

1. **Extend the base Adapter class** provided by the project.

   * Base Adapter class source: [`base.py`](https://github.com/openimis/openimis-be-api_etl_py/blob/release/24.10/api_etl/adapters/base.py)

2. **Create a new class** that inherits from this base class.

   * Reference implementation: [`exampleIndividialAdapter.py`](https://github.com/openimis/openimis-be-api_etl_py/blob/release/24.10/api_etl/adapters/exampleIndividialAdapter.py)

3. **Implement the required methods and logic** in your subclass based on the data structure and behavior needed.

4. **Register the adapter** in the appropriate place if required for discovery or configuration.

### Example

```python
from api_etl.adapters.base import BaseAdapter

class MyCustomAdapter(BaseAdapter):
    def transform(self, record):
        # Implement transformation logic here
        pass
```

This modular architecture allows easy customization and extension to support different data sources and APIs.
