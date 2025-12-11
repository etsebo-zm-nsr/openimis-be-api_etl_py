# flake8: noqa
from typing import Literal, Optional

from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider.base import AuthError, AuthProvider
from api_etl.auth_provider.basicAuthProvider import BasicAuthProvider
from api_etl.auth_provider.bearerAuthProvider import BearerAuthProvider
from api_etl.auth_provider.noAuthAuthProvider import NoAuthProvider

_auth_config_mapping = {
    "noauth": NoAuthProvider,
    "basic": BasicAuthProvider,
    "bearer": BearerAuthProvider,
}


def get_auth_provider(auth_type: Optional[Literal["noauth", "basic", "bearer"]] = None):
    auth_type = auth_type or ApiEtlConfig.auth_type
    if auth_type not in _auth_config_mapping:
        AuthError(f"Unknown auth type: {auth_type}")
    return _auth_config_mapping[auth_type]()
