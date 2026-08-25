"""Per-source configuration for the ETL core.

Upstream `ApiEtlConfig` loads a FLAT config onto class attributes, i.e. one URL, one
credential set and one field mapping per process - which is what prevented a second
source. This module replaces that with an immutable, per-source config object.

Each connector module owns its own `ModuleConfiguration` row (keyed by its Django app
label), so its config is flat, isolated and independently editable. The core owns only
the SHAPE: `SOURCE_DEFAULTS` plus the dataclasses below.

Defaults are applied at READ time, never in `AppConfig.ready()`, because
`ModuleConfiguration.get_or_default` is a *shallow* merge (`{**defaults, **db_config}`)
- a DB row supplying a partial `source` block would otherwise wipe the sibling keys.
"""
import copy
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SECRET_ENV_PREFIX = "env:"

# The group aggregation column is deliberately NOT configurable.
#
# individual's BaseGroupColumnAggregationClass routes `group_code` to
# _create_or_update_groups_using_group_code(), which looks up Group.objects.filter(code=...)
# GLOBALLY and unions members - the cross-source household merge this design depends on.
# ANY other value routes to _create_groups() -> generate_unique_code(), a random 8-char
# code regenerated on every run, which duplicates households on every sync.
GROUP_AGGREGATION_COLUMN = "group_code"


def resolve_secret(value: Any) -> Any:
    """Indirect a secret through the process environment.

    `ModuleConfiguration.config` is a plain TextField that lands in every database
    backup, and core's own model comment warns against storing credentials there. A
    config value of `"env:KOBO_API_TOKEN"` is read from the environment instead, so the
    real value lives only in the deployment's env.
    """
    if isinstance(value, str) and value.startswith(SECRET_ENV_PREFIX):
        env_name = value[len(SECRET_ENV_PREFIX):]
        resolved = os.environ.get(env_name)
        if resolved is None:
            logger.warning("api_etl: env var %r referenced in config is not set", env_name)
            return ""
        return resolved
    return value


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass(frozen=True)
class AuthConfig:
    type: str = "noauth"          # noauth | basic | bearer | token
    username: str = ""
    password: str = ""
    token: str = ""
    header: str = "Authorization"
    scheme: str = "Bearer"        # "Token" for KoboToolbox


@dataclass(frozen=True)
class ResponseConfig:
    """Where the payload lives inside the response envelope.

    Kept in config rather than code because external API contracts vary and are often
    not final when the connector is written - `{"rows": []}` vs `{"content": []}` vs a
    bare list should not require a code change.
    """
    rows_key: Optional[str] = None      # None => response body IS the list
    status_key: Optional[str] = None    # truthy-check key for API-level errors
    message_key: Optional[str] = None
    next_key: Optional[str] = None      # cursor/next-page URL (Kobo: "next")


@dataclass(frozen=True)
class SourceHttpConfig:
    http_method: str = "GET"
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    batch_size: int = 200
    timeout_seconds: int = 60
    max_pages: int = 0                  # 0 = unlimited; a runaway circuit breaker
    retry_total: int = 3
    retry_backoff_factor: float = 1.0
    verify_ssl: bool = True
    # Government APIs often present custom or self-signed certificates. Upstream api_etl
    # has no TLS configuration at all; Malawi's msr_etl added this for exactly that reason.
    ca_bundle_path: str = ""
    offset_param: str = "current"
    limit_param: str = "rowCount"
    response: ResponseConfig = field(default_factory=ResponseConfig)


@dataclass(frozen=True)
class AdapterConfig:
    # target column -> source path, "a/b/c" or "a.b.c" (Kobo emits "household/hoh_name")
    field_map: Dict[str, str] = field(default_factory=dict)
    constants: Dict[str, Any] = field(default_factory=dict)
    external_id_field: str = "id"
    external_id_prefix: str = ""        # namespace: "zispis:", "kobo:"
    group_code_field: Optional[str] = None
    group_code_prefix: str = ""
    role_field: Optional[str] = None
    role_map: Dict[str, str] = field(default_factory=dict)
    default_role: str = "OTHER_RELATIVE"
    recipient_field: Optional[str] = None
    national_id_field: Optional[str] = None
    national_id_type_field: Optional[str] = None
    date_formats: List[str] = field(default_factory=lambda: ["%Y-%m-%d"])


@dataclass(frozen=True)
class SinkConfig:
    lookup_field: str = "json_ext__external_id"
    update_existing: bool = True
    import_workflow: str = "Python Import Individuals"
    update_workflow: str = "Python Update Individuals"
    workflow_group: str = "individual"
    link_on_national_id: bool = True
    link_requires_secondary_match: bool = True


@dataclass(frozen=True)
class IncrementalConfig:
    enabled: bool = False
    mode: str = "none"                  # none | query_param | kobo_mongo
    cursor_field: Optional[str] = None  # e.g. "_submission_time"
    cursor_param: Optional[str] = None  # e.g. "updatedSince"
    overlap_minutes: int = 15
    initial_cursor: Optional[str] = None


@dataclass(frozen=True)
class ProvenanceConfig:
    data_source_label: str = ""         # -> json_ext.beneficiary_data_source
    source_type: str = ""               # -> IndividualDataSourceUpload.source_type
    batch_prefix: str = ""


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = False
    cron: Dict[str, Any] = field(default_factory=dict)   # kwargs for CronTrigger


@dataclass(frozen=True)
class SourceConfig:
    name: str
    enabled: bool = True
    auth: AuthConfig = field(default_factory=AuthConfig)
    source: SourceHttpConfig = field(default_factory=SourceHttpConfig)
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    sink: SinkConfig = field(default_factory=SinkConfig)
    incremental: IncrementalConfig = field(default_factory=IncrementalConfig)
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    @property
    def group_aggregation_column(self) -> str:
        """Always 'group_code' - see the constant's docstring."""
        return GROUP_AGGREGATION_COLUMN

    def with_overrides(self, **kwargs) -> "SourceConfig":
        return replace(self, **kwargs)


SOURCE_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "auth": {"type": "noauth", "username": "", "password": "", "token": "",
             "header": "Authorization", "scheme": "Bearer"},
    "source": {
        "http_method": "GET", "url": "", "headers": {}, "params": {},
        "batch_size": 200, "timeout_seconds": 60, "max_pages": 0,
        "retry_total": 3, "retry_backoff_factor": 1.0,
        "verify_ssl": True, "ca_bundle_path": "",
        "offset_param": "current", "limit_param": "rowCount",
        "response": {"rows_key": None, "status_key": None, "message_key": None, "next_key": None},
    },
    "adapter": {
        "field_map": {}, "constants": {}, "external_id_field": "id", "external_id_prefix": "",
        "group_code_field": None, "group_code_prefix": "", "role_field": None, "role_map": {},
        "default_role": "OTHER_RELATIVE", "recipient_field": None,
        "national_id_field": None, "national_id_type_field": None, "date_formats": ["%Y-%m-%d"],
    },
    "sink": {
        "lookup_field": "json_ext__external_id", "update_existing": True,
        "import_workflow": "Python Import Individuals",
        "update_workflow": "Python Update Individuals",
        "workflow_group": "individual",
        "link_on_national_id": True, "link_requires_secondary_match": True,
    },
    "incremental": {"enabled": False, "mode": "none", "cursor_field": None,
                    "cursor_param": None, "overlap_minutes": 15, "initial_cursor": None},
    "provenance": {"data_source_label": "", "source_type": "", "batch_prefix": ""},
    "schedule": {"enabled": False, "cron": {}},
}

_SECTIONS = {
    "auth": AuthConfig,
    "source": SourceHttpConfig,
    "adapter": AdapterConfig,
    "sink": SinkConfig,
    "incremental": IncrementalConfig,
    "provenance": ProvenanceConfig,
    "schedule": ScheduleConfig,
}

_SECRET_FIELDS = {("auth", "password"), ("auth", "token")}


def build_source_config(name: str, raw: Optional[dict] = None) -> SourceConfig:
    """Build an immutable SourceConfig from a raw (partial) config dict."""
    merged = deep_merge(SOURCE_DEFAULTS, raw or {})

    kwargs: Dict[str, Any] = {"name": name, "enabled": bool(merged.get("enabled", True))}
    for section, cls in _SECTIONS.items():
        values = dict(merged.get(section) or {})
        for secret_section, secret_key in _SECRET_FIELDS:
            if section == secret_section and secret_key in values:
                values[secret_key] = resolve_secret(values[secret_key])
        if section == "source":
            values["response"] = ResponseConfig(**_known(ResponseConfig, values.get("response") or {}))
        kwargs[section] = cls(**_known(cls, values))
    return SourceConfig(**kwargs)


def _known(cls, values: dict) -> dict:
    """Drop unknown keys so a stray config entry cannot crash startup."""
    allowed = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(values) - allowed
    if unknown:
        logger.warning("api_etl: ignoring unknown %s config keys: %s", cls.__name__, sorted(unknown))
    return {k: v for k, v in values.items() if k in allowed}


def read_module_config(module_name: str, layer: str = "be") -> dict:
    """Raw stored config for a module, or {} if there is no active row.

    Deliberately does NOT use `ModuleConfiguration.get_or_default`: that merges
    `{**defaults, **db_config}` one level deep, so a stored `{"source": {"url": ...}}`
    replaces the connector's ENTIRE `source` block rather than overriding one key
    (blocker G7). We need the raw row so it can be deep-merged instead.
    """
    from django.db.models import Q
    from django.utils import timezone

    from core.models import ModuleConfiguration
    try:
        row = ModuleConfiguration.objects.filter(
            Q(is_disabled_until=None) | Q(is_disabled_until__lt=timezone.now()),
            layer=layer, module=module_name,
        ).first()
    except Exception as exc:
        logger.error("api_etl: could not read %s configuration: %s", module_name, exc)
        return {}
    if not row or not row.config:
        return {}
    if isinstance(row.config, dict):
        return row.config
    try:
        import json
        return json.loads(row.config)
    except ValueError as exc:
        logger.error("api_etl: %s configuration is not valid JSON: %s", module_name, exc)
        return {}


def config_from_module(module_name: str, defaults: Optional[dict] = None) -> SourceConfig:
    """Read a connector's own ModuleConfiguration row and build its SourceConfig.

    This is what a connector's lazy `config_provider` should call. Import-time safe: the
    model import happens inside the function, so connectors can reference it from
    `AppConfig.ready()` without app-loading order concerns.

    Precedence, all deep-merged:
        SOURCE_DEFAULTS  <-  connector DEFAULT_CONFIG  <-  stored ModuleConfiguration
    """
    merged = deep_merge(defaults or {}, read_module_config(module_name))
    return build_source_config(module_name, merged)
