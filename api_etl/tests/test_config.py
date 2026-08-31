"""Tests for per-source configuration.

The single most important case here is `test_partial_db_override_keeps_siblings`:
blocker G7. `ModuleConfiguration.get_or_default` merges `{**defaults, **db_config}` one
level deep, so a stored `{"source": {"url": ...}}` would replace a connector's ENTIRE
source block - silently resetting batch_size, timeouts, TLS and the response envelope to
defaults. That bug was actually written and then caught; this test is what keeps it dead.
"""
import json
import os
from unittest import mock

from django.test import TestCase

from api_etl.config import (
    GROUP_AGGREGATION_COLUMN,
    SOURCE_DEFAULTS,
    build_source_config,
    config_from_module,
    deep_merge,
    read_module_config,
    resolve_secret,
)


class DeepMergeTestCase(TestCase):

    def test_nested_override_keeps_siblings(self):
        base = {"source": {"url": "a", "batch_size": 200, "timeout_seconds": 60}}
        merged = deep_merge(base, {"source": {"url": "b"}})
        self.assertEqual(merged["source"]["url"], "b")
        self.assertEqual(merged["source"]["batch_size"], 200)
        self.assertEqual(merged["source"]["timeout_seconds"], 60)

    def test_does_not_mutate_base(self):
        base = {"source": {"url": "a", "headers": {"X": "1"}}}
        deep_merge(base, {"source": {"url": "b", "headers": {"Y": "2"}}})
        self.assertEqual(base, {"source": {"url": "a", "headers": {"X": "1"}}})

    def test_non_dict_value_replaces_rather_than_merges(self):
        merged = deep_merge({"a": {"b": 1}}, {"a": "scalar"})
        self.assertEqual(merged["a"], "scalar")

    def test_none_override_is_tolerated(self):
        self.assertEqual(deep_merge({"a": 1}, None), {"a": 1})


class ResolveSecretTestCase(TestCase):

    def test_plain_value_passes_through(self):
        self.assertEqual(resolve_secret("literal"), "literal")

    def test_env_prefix_reads_environment(self):
        with mock.patch.dict(os.environ, {"ZM_TEST_SECRET": "s3cret"}):
            self.assertEqual(resolve_secret("env:ZM_TEST_SECRET"), "s3cret")

    def test_missing_env_var_yields_empty_not_the_literal(self):
        """A missing env var must not leak the string 'env:NAME' into an auth header."""
        os.environ.pop("ZM_TEST_ABSENT", None)
        self.assertEqual(resolve_secret("env:ZM_TEST_ABSENT"), "")

    def test_non_string_passes_through(self):
        self.assertEqual(resolve_secret(42), 42)
        self.assertIsNone(resolve_secret(None))


class BuildSourceConfigTestCase(TestCase):

    def test_defaults_only(self):
        cfg = build_source_config("x")
        self.assertEqual(cfg.name, "x")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.auth.type, "noauth")
        self.assertEqual(cfg.source.batch_size, SOURCE_DEFAULTS["source"]["batch_size"])
        self.assertIsNone(cfg.source.response.rows_key)

    def test_partial_source_override_keeps_siblings(self):
        """G7 in miniature, at the level build_source_config controls."""
        cfg = build_source_config("x", {"source": {"url": "https://api.example/records"}})
        self.assertEqual(cfg.source.url, "https://api.example/records")
        self.assertEqual(cfg.source.batch_size, 200)
        self.assertEqual(cfg.source.timeout_seconds, 60)
        self.assertTrue(cfg.source.verify_ssl)

    def test_partial_response_envelope_override_keeps_siblings(self):
        cfg = build_source_config("x", {"source": {"response": {"rows_key": "results"}}})
        self.assertEqual(cfg.source.response.rows_key, "results")
        self.assertIsNone(cfg.source.response.next_key)

    def test_secret_fields_are_resolved(self):
        with mock.patch.dict(os.environ, {"ZM_TEST_TOKEN": "tok"}):
            cfg = build_source_config("x", {"auth": {"type": "token", "token": "env:ZM_TEST_TOKEN"}})
        self.assertEqual(cfg.auth.token, "tok")

    def test_unknown_keys_are_dropped_not_fatal(self):
        """A stray config key must not crash startup for every connector."""
        cfg = build_source_config("x", {"source": {"nonsense_key": 1, "url": "u"}})
        self.assertEqual(cfg.source.url, "u")
        self.assertFalse(hasattr(cfg.source, "nonsense_key"))

    def test_config_is_immutable(self):
        cfg = build_source_config("x")
        with self.assertRaises(Exception):
            cfg.source.url = "mutated"

    def test_group_aggregation_column_is_not_configurable(self):
        """G5: any value other than 'group_code' regenerates household codes every run."""
        cfg = build_source_config("x", {"group_aggregation_column": "household_ref",
                                        "sink": {"group_aggregation_column": "nope"}})
        self.assertEqual(cfg.group_aggregation_column, GROUP_AGGREGATION_COLUMN)
        self.assertEqual(cfg.group_aggregation_column, "group_code")

    def test_with_overrides_returns_a_new_config(self):
        cfg = build_source_config("x")
        renamed = cfg.with_overrides(name="y")
        self.assertEqual(cfg.name, "x")
        self.assertEqual(renamed.name, "y")


class ConfigFromModuleTestCase(TestCase):
    """The real precedence chain, against a real ModuleConfiguration row."""

    MODULE = "zm_test_connector"

    CONNECTOR_DEFAULTS = {
        "source": {
            "url": "https://default.example/api",
            "batch_size": 500,
            "timeout_seconds": 90,
            "response": {"rows_key": "rows"},
        },
        "adapter": {"external_id_prefix": "test:"},
    }

    def _store(self, config: dict):
        from core.models import ModuleConfiguration
        ModuleConfiguration.objects.create(
            module=self.MODULE, layer="be", version="1", config=json.dumps(config),
        )

    def test_no_row_falls_back_to_connector_defaults(self):
        cfg = config_from_module(self.MODULE, self.CONNECTOR_DEFAULTS)
        self.assertEqual(cfg.source.url, "https://default.example/api")
        self.assertEqual(cfg.source.batch_size, 500)
        self.assertEqual(cfg.adapter.external_id_prefix, "test:")

    def test_partial_db_override_keeps_siblings(self):
        """G7, end to end - the regression this whole function exists for.

        A stored row supplying only `source.url` must override that one key and leave
        batch_size / timeout_seconds / the response envelope from the connector defaults
        intact. `get_or_default`'s shallow merge would blank all of them.
        """
        self._store({"source": {"url": "https://override.example/api"}})

        cfg = config_from_module(self.MODULE, self.CONNECTOR_DEFAULTS)

        self.assertEqual(cfg.source.url, "https://override.example/api")
        self.assertEqual(cfg.source.batch_size, 500, "sibling key wiped by a shallow merge")
        self.assertEqual(cfg.source.timeout_seconds, 90, "sibling key wiped by a shallow merge")
        self.assertEqual(cfg.source.response.rows_key, "rows", "nested block wiped")
        self.assertEqual(cfg.adapter.external_id_prefix, "test:", "sibling section wiped")

    def test_db_row_wins_over_connector_default(self):
        self._store({"source": {"batch_size": 25}})
        cfg = config_from_module(self.MODULE, self.CONNECTOR_DEFAULTS)
        self.assertEqual(cfg.source.batch_size, 25)

    def test_disabled_row_is_ignored(self):
        """is_disabled_until in the future means the row is not active config."""
        from django.utils import timezone
        from datetime import timedelta

        from core.models import ModuleConfiguration
        ModuleConfiguration.objects.create(
            module=self.MODULE, layer="be", version="1",
            config=json.dumps({"source": {"url": "https://disabled.example"}}),
            is_disabled_until=timezone.now() + timedelta(days=1),
        )
        cfg = config_from_module(self.MODULE, self.CONNECTOR_DEFAULTS)
        self.assertEqual(cfg.source.url, "https://default.example/api")

    def test_malformed_json_does_not_raise(self):
        """A bad row must degrade to defaults, not break every ETL page load.

        `ModuleConfiguration.clean()` rejects invalid JSON on save, so this cannot be
        reached through the ORM - hence the queryset `update()`, which bypasses
        save()/clean() exactly the way a direct SQL edit or a restored backup would.
        """
        from core.models import ModuleConfiguration
        self._store({"source": {"url": "https://valid.example"}})
        ModuleConfiguration.objects.filter(module=self.MODULE).update(config="{not json")

        self.assertEqual(read_module_config(self.MODULE), {})
        cfg = config_from_module(self.MODULE, self.CONNECTOR_DEFAULTS)
        self.assertEqual(cfg.source.url, "https://default.example/api")

    def test_name_defaults_to_the_module_label(self):
        cfg = config_from_module(self.MODULE, self.CONNECTOR_DEFAULTS)
        self.assertEqual(cfg.name, self.MODULE)
