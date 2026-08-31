"""Tests for the connector registry.

The registry is the extension point the whole core/connector architecture rests on:
it is what lets a service class living in `zm_etl_kobo` be discovered, which upstream's
`get_classes_in_module("api_etl.services")` introspection could never do.

Two properties matter most and are easy to regress:

* `build_config()` forces `SourceConfig.name` to the REGISTRY name. A connector builds
  its config with `config_from_module(MODULE_NAME, ...)`, naming it after the Django app
  label ("zm_etl_zispis") while operators, the scheduler and `etl_reset_cursor` all use
  the registry name ("zispis"). When these drifted, sync state was written under one key
  and read under the other, and incremental sync silently never resumed.
* One broken connector must not take the listing down with it. That isolation is the
  reason the module split was chosen over a single multi-source module.
"""
from django.test import TestCase

from api_etl.config import build_source_config
from api_etl.registry import (
    EtlSourceRegistration,
    RegistryError,
    get_etl_source,
    list_etl_sources,
    register_etl_source,
    registered_names,
    unregister_etl_source,
)


class _ServiceA:
    pass


class _ServiceB:
    pass


class RegistryTestCase(TestCase):

    def setUp(self):
        self.registered = []

    def tearDown(self):
        for name in self.registered:
            unregister_etl_source(name)

    def _register(self, name, service_cls=_ServiceA, provider=None, **kw):
        self.registered.append(name)
        return register_etl_source(
            name, service_cls, provider or (lambda: build_source_config(name)), **kw
        )

    # ------------------------------------------------------------------ basics

    def test_register_and_get(self):
        self._register("alpha", label="Alpha source")
        registration = get_etl_source("alpha")
        self.assertIsNotNone(registration)
        self.assertEqual(registration.service_cls, _ServiceA)
        self.assertEqual(registration.display_name, "Alpha source")

    def test_display_name_falls_back_to_class_name(self):
        self._register("alpha")
        self.assertEqual(get_etl_source("alpha").display_name, "_ServiceA")

    def test_unknown_name_returns_none(self):
        self.assertIsNone(get_etl_source("nope"))

    def test_empty_name_rejected(self):
        with self.assertRaises(RegistryError):
            register_etl_source("", _ServiceA, lambda: None)

    # ------------------------------------------------------- lazy config_provider

    def test_non_callable_config_provider_rejected(self):
        """A resolved config would bind app-loading order into the registry."""
        with self.assertRaises(RegistryError) as ctx:
            register_etl_source("eager", _ServiceA, build_source_config("eager"))
        self.assertIn("callable", str(ctx.exception))

    def test_config_provider_is_not_called_at_registration(self):
        calls = []

        def provider():
            calls.append(1)
            return build_source_config("lazy")

        self._register("lazy", provider=provider)
        self.assertEqual(calls, [], "provider was resolved eagerly at registration")

        get_etl_source("lazy").build_config()
        self.assertEqual(calls, [1])

    def test_config_is_resolved_per_call_so_edits_need_no_restart(self):
        counter = {"n": 0}

        def provider():
            counter["n"] += 1
            return build_source_config("fresh", {"source": {"batch_size": counter["n"]}})

        self._register("fresh", provider=provider)
        first = get_etl_source("fresh").build_config().source.batch_size
        second = get_etl_source("fresh").build_config().source.batch_size
        self.assertEqual((first, second), (1, 2))

    # --------------------------------------------------------- duplicate handling

    def test_duplicate_name_from_a_different_class_is_an_error(self):
        self._register("dup", _ServiceA)
        with self.assertRaises(RegistryError) as ctx:
            register_etl_source("dup", _ServiceB, lambda: None)
        self.assertIn("already registered", str(ctx.exception))

    def test_re_registering_the_same_class_is_idempotent(self):
        """AppConfig.ready() legitimately runs twice under runserver autoreload."""
        first = self._register("idem", _ServiceA)
        second = register_etl_source("idem", _ServiceA, lambda: None)
        self.assertIs(first, second)

    def test_replace_flag_allows_deliberate_override(self):
        self._register("repl", _ServiceA)
        register_etl_source("repl", _ServiceB, lambda: build_source_config("repl"), replace=True)
        self.assertEqual(get_etl_source("repl").service_cls, _ServiceB)

    # ------------------------------------------------------------ name authority

    def test_build_config_forces_the_registry_name(self):
        """The bug that made sync state silently never resume.

        The connector names its config after the Django app label; the registry name is
        what the scheduler, sync state and etl_reset_cursor use. They must not drift.
        """
        self._register(
            "shortname", provider=lambda: build_source_config("zm_etl_longmodulename"),
        )
        config = get_etl_source("shortname").build_config()
        self.assertEqual(config.name, "shortname")

    def test_real_connectors_agree_on_their_own_name(self):
        """Guards the live registrations, not just a synthetic one."""
        for name in ("zispis", "kobo"):
            registration = get_etl_source(name)
            if registration is None:      # connector not installed in this assembly
                continue
            self.assertEqual(registration.build_config().name, name)

    def test_build_config_leaves_a_matching_name_alone(self):
        self._register("match", provider=lambda: build_source_config("match"))
        self.assertEqual(get_etl_source("match").build_config().name, "match")

    def test_build_config_tolerates_a_provider_returning_none(self):
        registration = EtlSourceRegistration("n", _ServiceA, lambda: None)
        self.assertIsNone(registration.build_config())

    # ---------------------------------------------------------------- listing

    def test_listing_is_sorted_and_excludes_disabled(self):
        self._register("bbb")
        self._register("aaa")
        self._register("off", provider=lambda: build_source_config("off", {"enabled": False}))

        names = registered_names()
        self.assertEqual([n for n in names if n in ("aaa", "bbb", "off")], ["aaa", "bbb"])

    def test_include_disabled_returns_disabled_sources(self):
        self._register("off2", provider=lambda: build_source_config("off2", {"enabled": False}))
        self.assertIn("off2", registered_names(include_disabled=True))

    def test_a_broken_connector_does_not_break_the_listing(self):
        """The isolation property the module split was chosen for."""
        def explode():
            raise RuntimeError("missing env var / bad config")

        self._register("healthy")
        self._register("broken", provider=explode)

        names = registered_names()
        self.assertIn("healthy", names)
        self.assertNotIn("broken", names)

    def test_include_disabled_skips_config_building_entirely(self):
        """Ops needs to see a broken source exists, which means not building its config."""
        def explode():
            raise RuntimeError("boom")

        self._register("brokentoo", provider=explode)
        self.assertIn("brokentoo", registered_names(include_disabled=True))

    def test_list_returns_registration_objects(self):
        self._register("obj")
        found = [r for r in list_etl_sources() if r.name == "obj"]
        self.assertEqual(len(found), 1)
        self.assertIsInstance(found[0], EtlSourceRegistration)

    # ------------------------------------------------------------ trigger perms

    def test_trigger_perms_are_copied_not_aliased(self):
        perms = ["953101"]
        self._register("perm", trigger_perms=perms)
        perms.append("999999")
        self.assertEqual(get_etl_source("perm").trigger_perms, ["953101"])

    def test_trigger_perms_default_to_none(self):
        self._register("noperm")
        self.assertIsNone(get_etl_source("noperm").trigger_perms)
