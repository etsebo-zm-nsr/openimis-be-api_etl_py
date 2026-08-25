"""Registry of ETL connectors.

Upstream discovers services by introspecting the `api_etl.services` package:

    get_classes_in_module("api_etl.services")   # filters on module_name in cls.__module__

which can only ever see classes defined inside `api_etl` itself. A service living in
`zm_etl_kobo.services` fails that filter, so package introspection cannot support
connectors shipped as separate Django modules.

This registry replaces it. A connector registers itself in its `AppConfig.ready()`:

    from api_etl.registry import register_etl_source
    from api_etl.config import config_from_module

    register_etl_source(
        name="zispis",
        service_cls=ZispisETLService,
        config_provider=lambda: config_from_module(MODULE_NAME, DEFAULT_CONFIG),
        label="ZISPIS (external MIS)",
        trigger_perms=["953101"],
    )

The same pattern openIMIS already uses for workflows
(`individual/apps.py` -> `PythonWorkflowAdaptor.register_workflow`).

`config_provider` MUST be a callable, resolved lazily at run time. That keeps the
registry free of app-loading-order constraints, and means a connector's configuration
can be changed in `ModuleConfiguration` and picked up without a restart.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class EtlSourceRegistration:
    name: str
    service_cls: Type
    config_provider: Callable[[], "object"]
    label: Optional[str] = None
    trigger_perms: Optional[List[str]] = None

    @property
    def display_name(self) -> str:
        return self.label or self.service_cls.__name__

    def build_config(self):
        """Resolve this connector's configuration. Lazy by contract.

        The registry name is authoritative for `SourceConfig.name`. A connector normally
        builds its config with `config_from_module(MODULE_NAME, ...)`, which names it
        after the Django app label - so a connector registered as "zispis" from module
        "zm_etl_zispis" would otherwise key its sync state under the module name while
        operators, the scheduler and `etl_reset_cursor` all use the registry name.
        Forcing it here means the two can never drift.
        """
        config = self.config_provider()
        if config is not None and getattr(config, "name", None) != self.name:
            config = config.with_overrides(name=self.name)
        return config


_REGISTRY: Dict[str, EtlSourceRegistration] = {}


def register_etl_source(name, service_cls, config_provider, *, label=None,
                        trigger_perms=None, replace=False) -> EtlSourceRegistration:
    if not name:
        raise RegistryError("register_etl_source: name is required")
    if not callable(config_provider):
        raise RegistryError(
            f"register_etl_source({name!r}): config_provider must be a callable returning "
            "a SourceConfig, so it can be resolved lazily at run time"
        )
    if name in _REGISTRY and not replace:
        existing = _REGISTRY[name].service_cls
        if existing is service_cls:
            # AppConfig.ready() can legitimately run twice (e.g. runserver autoreload).
            return _REGISTRY[name]
        raise RegistryError(
            f"ETL source {name!r} is already registered by {existing.__module__}.{existing.__name__}"
        )

    registration = EtlSourceRegistration(
        name=name,
        service_cls=service_cls,
        config_provider=config_provider,
        label=label,
        trigger_perms=list(trigger_perms) if trigger_perms else None,
    )
    _REGISTRY[name] = registration
    logger.info("api_etl: registered ETL source %r -> %s", name, service_cls.__name__)
    return registration


def unregister_etl_source(name: str) -> None:
    """Remove a registration. Intended for tests."""
    _REGISTRY.pop(name, None)


def get_etl_source(name: str) -> Optional[EtlSourceRegistration]:
    return _REGISTRY.get(name)


def list_etl_sources(include_disabled: bool = False) -> List[EtlSourceRegistration]:
    """Registered connectors, sorted by name.

    A connector whose config cannot be built (bad JSON, missing env var) must not take
    the whole listing down with it - that would defeat the isolation the module split
    was chosen for. Such a connector is skipped and logged.
    """
    out = []
    for name in sorted(_REGISTRY):
        registration = _REGISTRY[name]
        if include_disabled:
            out.append(registration)
            continue
        try:
            if registration.build_config().enabled:
                out.append(registration)
        except Exception as exc:
            logger.error("api_etl: skipping source %r - config failed to build: %s",
                         name, exc, exc_info=exc)
    return out


def registered_names(include_disabled: bool = False) -> List[str]:
    return [r.name for r in list_etl_sources(include_disabled=include_disabled)]
