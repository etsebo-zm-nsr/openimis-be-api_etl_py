"""Resolution of an ETL service by name.

Every triggerable service is a connector registered via `api_etl.registry`. There is no
other source of truth - a service that isn't registered doesn't exist as far as the
GraphQL schema, the FE's *Import Data - API* page, or the scheduler are concerned.
"""
from typing import List, Optional, Tuple

from api_etl.registry import get_etl_source, list_etl_sources


def available_service_names() -> List[str]:
    """Every triggerable service name, in registration order."""
    return [registration.name for registration in list_etl_sources()]


def resolve_service(name: str) -> Tuple[Optional[type], Optional[object]]:
    """Resolve `name` to (service_cls, registration), or (None, None) if unregistered."""
    registration = get_etl_source(name)
    if registration is None:
        return None, None
    return registration.service_cls, registration


def build_service(name: str, user):
    """Instantiate the registered service for `name` with its resolved config."""
    service_cls, registration = resolve_service(name)
    if service_cls is None:
        raise ValueError(f"There is no ETL service named {name!r}")
    return service_cls(user, config=registration.build_config())
