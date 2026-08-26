import base64

from api_etl.apps import ApiEtlConfig
from api_etl.auth_provider.base import AuthProvider, AuthError


class BasicAuthProvider(AuthProvider):
    """
    Auth provider that add basic token authorization header for the request
    """

    def __init__(self, auth_cfg=None):
        # auth_cfg is an api_etl.config.AuthConfig, resolved per-connector by the
        # registry. None falls back to the process-global ApiEtlConfig.
        self.auth_cfg = auth_cfg

    def get_auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Basic {self._get_token_value()}"}

    def _get_token_value(self):
        if self.auth_cfg is not None:
            username, password = self.auth_cfg.username, self.auth_cfg.password
        else:
            username, password = ApiEtlConfig.auth_basic_username, ApiEtlConfig.auth_basic_password
        if not username or not password:
            raise AuthError("Basic auth credentials not provided")
        basic_payload = f"{username}:{password}"
        return base64.b64encode(basic_payload.encode("utf-8")).decode("utf-8")
