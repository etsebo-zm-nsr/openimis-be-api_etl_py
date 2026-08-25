from api_etl.auth_provider.base import AuthProvider, AuthError


class TokenAuthProvider(AuthProvider):
    """Auth provider for APIs using a non-Bearer token scheme.

    KoboToolbox expects `Authorization: Token <token>`, not `Bearer`. The scheme and
    header name are configurable so DRF `Token`, `ApiKey` and similar variants are all
    covered without another provider class.
    """

    def __init__(self, auth_cfg=None):
        self.auth_cfg = auth_cfg

    def get_auth_header(self) -> dict[str, str]:
        if self.auth_cfg is None:
            raise AuthError("Token auth requires a source configuration")
        token = self.auth_cfg.token
        if not token:
            raise AuthError("Token not provided")
        scheme = (self.auth_cfg.scheme or "").strip()
        header = self.auth_cfg.header or "Authorization"
        value = f"{scheme} {token}".strip() if scheme else token
        return {header: value}
