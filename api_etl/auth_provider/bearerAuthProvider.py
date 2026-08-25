from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider.base import AuthProvider, AuthError


class BearerAuthProvider(AuthProvider):
    """
    Auth provider that add bearer token authorization header for the request
    """

    def __init__(self, auth_cfg=None):
        # See BasicAuthProvider: None preserves the upstream global-config behaviour.
        self.auth_cfg = auth_cfg

    def get_auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token_value()}"}

    def _get_token_value(self):
        token = self.auth_cfg.token if self.auth_cfg is not None else ApiEtlConfig.auth_bearer_token
        if not token:
            raise AuthError("Bearer token not provided")
        return token
