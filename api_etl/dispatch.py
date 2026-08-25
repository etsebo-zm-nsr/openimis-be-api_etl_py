"""Resolution of an ETL service by name.

Two sources of truth coexist deliberately:

  * the registry (`api_etl.registry`) - connectors shipped as their own Django modules;
  * package introspection (`get_classes_in_module("api_etl.services")`) - the upstream
    mechanism, which can only see classes defined inside `api_etl` itself.

Keeping both means upstream's ExampleIndividual* pipeline and its tests continue to work
unchanged while connectors are added, and an upstream rebase does not conflict here.
"""
import logging
from typing import List, Optional, Tuple

from api_etl.registry import get_etl_source, list_etl_sources
from api_etl.utils import ETL_CLASS, get_class_by_name, get_classes_in_module

logger = logging.getLogger(__name__)


# Base classes that live in api_etl.services but are not themselves runnable.
# get_classes_in_module() only excludes api_etl.services.base, so anything else exported
# from the package would otherwise be offered to users as a triggerable service.
ABSTRACT_SERVICES = {"ETLService", "ConnectorETLService"}


def available_service_names() -> List[str]:
    """Every triggerable service name: registered connectors first, then legacy classes."""
    names = [registration.name for registration in list_etl_sources()]
    try:
        legacy = get_classes_in_module(ETL_CLASS)
    except ImportError:
        legacy = []
    names.extend(name for name in legacy
                 if name not in names and name not in ABSTRACT_SERVICES)
    return names


def resolve_service(name: str) -> Tuple[Optional[type], Optional[object]]:
    """Resolve `name` to (service_cls, registration).

    `registration` is None for a legacy in-package service.
    """
    registration = get_etl_source(name)
    if registration is not None:
        return registration.service_cls, registration
    try:
        return get_class_by_name(ETL_CLASS, name), None
    except ImportError:
        return None, None


def build_service(name: str, user):
    """Instantiate the service for `name`.

    Registered connectors receive their resolved config; legacy services keep the
    upstream `(user)` signature.
    """
    service_cls, registration = resolve_service(name)
    if service_cls is None:
        raise ValueError(f"There is no ETL service named {name!r}")
    if registration is None:
        return service_cls(user)
    return service_cls(user, config=registration.build_config())
