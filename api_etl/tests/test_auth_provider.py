"""Tests for auth providers.

The property that matters most: an UNSET credential must raise, never silently produce
an unauthenticated request. Connectors ship with empty credentials by design
(`"password": "env:ZISPIS_API_PASSWORD"` resolving to "" until the deployment sets it),
so "empty credential" is the normal pre-configuration state of every environment - and
it must fail closed.
"""
import os
from unittest import mock

from django.test import TestCase

from api_etl.auth_provider import get_auth_provider
from api_etl.auth_provider.base import AuthError
from api_etl.auth_provider.noAuthAuthProvider import NoAuthProvider
from api_etl.auth_provider.tokenAuthProvider import TokenAuthProvider
from api_etl.config import build_source_config


def _auth(**auth):
    return build_source_config("t", {"auth": auth}).auth


class ProviderSelectionTestCase(TestCase):

    def test_type_comes_from_the_config_slice(self):
        self.assertIsInstance(get_auth_provider(auth_cfg=_auth(type="noauth")), NoAuthProvider)
        self.assertIsInstance(get_auth_provider(auth_cfg=_auth(type="token", token="x")),
                              TokenAuthProvider)

    def test_unknown_auth_type_raises_rather_than_keyerror(self):
        with self.assertRaises(AuthError) as ctx:
            get_auth_provider(auth_cfg=_auth(type="wat"))
        self.assertIn("wat", str(ctx.exception))


class FailClosedTestCase(TestCase):
    """No credential => no request. Never an unauthenticated pull."""

    def test_basic_without_credentials_raises(self):
        provider = get_auth_provider(auth_cfg=_auth(type="basic"))
        with self.assertRaises(AuthError):
            provider.get_auth_header()

    def test_basic_with_only_a_username_raises(self):
        provider = get_auth_provider(auth_cfg=_auth(type="basic", username="u"))
        with self.assertRaises(AuthError):
            provider.get_auth_header()

    def test_bearer_without_token_raises(self):
        provider = get_auth_provider(auth_cfg=_auth(type="bearer"))
        with self.assertRaises(AuthError):
            provider.get_auth_header()

    def test_token_without_token_raises(self):
        provider = get_auth_provider(auth_cfg=_auth(type="token"))
        with self.assertRaises(AuthError):
            provider.get_auth_header()

    def test_unresolved_env_secret_fails_closed(self):
        """The realistic deployment mistake: the env var was never set.

        resolve_secret returns "" for a missing var, so this must land on the
        empty-credential path and raise - not send the literal "env:NAME".
        """
        os.environ.pop("ZM_TEST_UNSET_TOKEN", None)
        cfg = _auth(type="token", token="env:ZM_TEST_UNSET_TOKEN")
        self.assertEqual(cfg.token, "")
        with self.assertRaises(AuthError):
            get_auth_provider(auth_cfg=cfg).get_auth_header()

    def test_the_literal_env_string_never_reaches_a_header(self):
        os.environ.pop("ZM_TEST_UNSET_TOKEN", None)
        cfg = _auth(type="token", token="env:ZM_TEST_UNSET_TOKEN")
        self.assertNotIn("env:", cfg.token)


class HeaderShapeTestCase(TestCase):

    def test_noauth_sends_nothing(self):
        self.assertEqual(get_auth_provider(auth_cfg=_auth(type="noauth")).get_auth_header(), {})

    def test_basic_is_base64_of_user_colon_password(self):
        import base64
        provider = get_auth_provider(auth_cfg=_auth(type="basic", username="u", password="p"))
        expected = base64.b64encode(b"u:p").decode()
        self.assertEqual(provider.get_auth_header(), {"Authorization": f"Basic {expected}"})

    def test_bearer_shape(self):
        provider = get_auth_provider(auth_cfg=_auth(type="bearer", token="tok"))
        self.assertEqual(provider.get_auth_header(), {"Authorization": "Bearer tok"})

    def test_token_scheme_is_configurable_for_kobo(self):
        """KoboToolbox requires `Token <t>`, not `Bearer <t>`."""
        provider = get_auth_provider(auth_cfg=_auth(type="token", token="tok", scheme="Token"))
        self.assertEqual(provider.get_auth_header(), {"Authorization": "Token tok"})

    def test_custom_header_name(self):
        provider = get_auth_provider(
            auth_cfg=_auth(type="token", token="tok", scheme="", header="X-Api-Key"))
        self.assertEqual(provider.get_auth_header(), {"X-Api-Key": "tok"})

    def test_env_resolved_secret_reaches_the_header(self):
        with mock.patch.dict(os.environ, {"ZM_TEST_SET_TOKEN": "real-token"}):
            cfg = _auth(type="token", token="env:ZM_TEST_SET_TOKEN", scheme="Token")
        self.assertEqual(get_auth_provider(auth_cfg=cfg).get_auth_header(),
                         {"Authorization": "Token real-token"})
