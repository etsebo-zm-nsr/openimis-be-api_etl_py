# flake8: noqa
from typing import Literal, Optional

from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider.base import AuthError, AuthProvider
from api_etl.auth_provider.basicAuthProvider import BasicAuthProvider
from api_etl.auth_provider.bearerAuthProvider import BearerAuthProvider
from api_etl.auth_provider.noAuthAuthProvider import NoAuthProvider
from api_etl.auth_provider.tokenAuthProvider import TokenAuthProvider

_auth_config_mapping = {
    "noauth": NoAuthProvider,
    "basic": BasicAuthProvider,
    "bearer": BearerAuthProvider,
    "token": TokenAuthProvider,
}


def get_auth_provider(auth_type: Optional[Literal["noauth", "basic", "bearer", "token"]] = None,
                      auth_cfg=None) -> AuthProvider:
    """Build an AuthProvider.

    Two call styles:
      * `get_auth_provider(auth_cfg=cfg.auth)` - registry-based connectors; the type and
        credentials both come from the per-source config slice. Used by every connector.
      * `get_auth_provider()` / `get_auth_provider("basic")` - falls back to the
        process-global ApiEtlConfig when no per-source config is given.
    """
    if auth_cfg is not None and auth_type is None:
        auth_type = auth_cfg.type
    auth_type = auth_type or ApiEtlConfig.auth_type

    provider_cls = _auth_config_mapping.get(auth_type)
    if provider_cls is None:
        # Upstream constructed this exception without raising it, so an unknown auth type
        # fell through to a KeyError instead. Raise it, so a typo reports what it is.
        raise AuthError(f"Unknown auth type: {auth_type}")

    if provider_cls is NoAuthProvider:
        return provider_cls()
    return provider_cls(auth_cfg)
