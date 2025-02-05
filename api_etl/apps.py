from django.apps import AppConfig

MODULE_NAME = "api_etl"

DEFAULT_CONFIG = {
    "auth_type": "basic",  # noauth, basic, bearer
    "auth_basic_username": "",  # basic auth username
    "auth_basic_password": "",  # basic auth password
    "auth_bearer_token": "",  # bearer token

    "source_http_method": "",  # valid input for requests.request required
    "source_url": "",
    "source_headers": {},
    "source_batch_size": 50,

    "adapter_first_name_field": "firstName",
    "adapter_last_name_field": "lastName",
    "adapter_dob_field": "dateOfBirth",
    "adapter_location_name_field": "locationName",
    "adapter_location_code_field": "locationCode",

    "gql_query_api_etl_rule_perms": ["953001"],
    "gql_mutation_execute_api_etl_rule_perms": ["953002"],
}


class ApiEtlConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    auth_type = None
    auth_basic_username = None
    auth_basic_password = None
    auth_bearer_token = None

    source_http_method = None
    source_url = None
    source_headers = None
    source_batch_size = None

    adapter_first_name_field = None
    adapter_last_name_field = None
    adapter_dob_field = None
    adapter_location_name_field = None
    adapter_location_code_field = None

    gql_query_api_etl_rule_perms = None
    gql_mutation_execute_api_etl_rule_perms = None

    @classmethod
    def _load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(ApiEtlConfig, field):
                setattr(ApiEtlConfig, field, cfg[field])

    def ready(self):
        from core.models import ModuleConfiguration
        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self._load_config(cfg)
