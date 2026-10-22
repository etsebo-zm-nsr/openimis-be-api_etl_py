"""Held-back records must be persisted, not just logged.

Before `ETLHeldRecord` a record the adapter could not import was counted in
`records_skipped` and then forgotten, so nobody could find the people missing from the
registry. These tests cover the persistence, the sighting counters, resolution, and that
persistence can never break a sync.
"""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from api_etl.adapters.base_mapping_adapter import BaseMappingAdapter
from api_etl.config import build_source_config
from api_etl.models import ETLHeldRecord
from api_etl.services.base import ETLService, sanitise_for_jsonb


class _Source:
    observed_cursor = None

    def __init__(self, batches):
        self.batches = batches

    def pull(self):
        for i, batch in enumerate(self.batches):
            yield batch, i


class _Sink:
    def push(self, batch, identifier):
        self.last = list(batch)


def _adapter():
    cfg = build_source_config("held-src", {"adapter": {
        "field_map": {"first_name": "fn", "last_name": "ln", "dob": "d"},
        "external_id_field": "id",
        # dob is not required by default (it is nullable in the registry); these tests
        # exercise the hold-back mechanism and need some required column to trip it.
        "required_fields": ["first_name", "last_name", "dob"],
    }})
    return BaseMappingAdapter(cfg)


def _run(batches, adapter=None):
    service = ETLService(_Source(batches), adapter or _adapter(), _Sink())
    return service.execute()


GOOD = {"id": "1", "fn": "Grace", "ln": "Hopper", "d": "1906-12-09"}
BAD = {"id": "2", "fn": "Ada", "ln": "Lovelace", "d": None}


class SanitiseTestCase(TestCase):

    def test_lone_surrogates_replaced_in_keys_and_values(self):
        out = sanitise_for_jsonb({"k\ud819": ["a\ud819b", {"n": "x\udfffy"}]})
        self.assertEqual(out, {"k�": ["a�b", {"n": "x�y"}]})

    def test_nul_stripped_from_keys_and_values(self):
        out = sanitise_for_jsonb({"k\x00": ["Ban\x00da"]})
        self.assertEqual(out, {"k": ["Banda"]})

    def test_nan_and_unknown_types(self):
        out = sanitise_for_jsonb({"a": float("nan"), "b": 1.5, "c": object, "d": None})
        self.assertIsNone(out["a"])
        self.assertEqual(out["b"], 1.5)
        self.assertIsInstance(out["c"], str)


class HeldPersistenceTestCase(TestCase):

    def test_held_row_is_persisted_with_sanitised_payload(self):
        bad = dict(BAD, fn="Ad\ud819a")
        result = _run([[GOOD, bad]])
        self.assertTrue(result["success"])
        held = ETLHeldRecord.objects.get(source_name="held-src", external_id="2")
        self.assertEqual(held.reasons, ["dob"])
        self.assertEqual(held.raw_payload["fn"], "Ad�a")
        self.assertEqual(held.times_seen, 1)
        self.assertIsNone(held.resolved_at)
        self.assertEqual(result["data"]["records_skipped"], 1)
        self.assertEqual(result["data"]["records_held_open"], 1)
        self.assertFalse(ETLHeldRecord.objects.filter(external_id="1").exists())

    def test_second_sighting_increments_and_updates(self):
        _run([[BAD]])
        first = ETLHeldRecord.objects.get(external_id="2")
        _run([[dict(BAD, fn="Changed")]])
        again = ETLHeldRecord.objects.get(external_id="2")
        self.assertEqual(again.times_seen, 2)
        self.assertEqual(again.raw_payload["fn"], "Changed")
        self.assertGreater(again.last_seen, first.last_seen)
        self.assertEqual(again.first_seen, first.first_seen)

    def test_earlier_batches_are_not_rewritten(self):
        _run([[BAD], [GOOD]])
        self.assertEqual(ETLHeldRecord.objects.get(external_id="2").times_seen, 1)

    def test_later_import_resolves(self):
        _run([[BAD]])
        fixed = dict(BAD, d="1950-12-10")
        result = _run([[fixed]])
        held = ETLHeldRecord.objects.get(external_id="2")
        self.assertIsNotNone(held.resolved_at)
        self.assertEqual(result["data"]["records_held_open"], 0)

    def test_reheld_after_resolution_clears_resolved_at(self):
        _run([[BAD]])
        _run([[dict(BAD, d="1950-12-10")]])
        self.assertIsNotNone(ETLHeldRecord.objects.get(external_id="2").resolved_at)
        _run([[BAD]])
        held = ETLHeldRecord.objects.get(external_id="2")
        self.assertIsNone(held.resolved_at)
        self.assertEqual(held.times_seen, 2)

    def test_resolution_is_scoped_to_the_source(self):
        ETLHeldRecord.objects.create(
            source_name="other", external_id="1", reasons=["dob"], raw_payload={},
            first_seen="2026-01-01T00:00:00Z", last_seen="2026-01-01T00:00:00Z")
        _run([[GOOD]])
        self.assertIsNone(ETLHeldRecord.objects.get(source_name="other").resolved_at)

    def test_persistence_failure_does_not_break_execute(self):
        with mock.patch.object(ETLHeldRecord.objects, "bulk_create",
                               side_effect=RuntimeError("db down")):
            with self.assertLogs("api_etl.services.base", level="ERROR"):
                result = _run([[GOOD, BAD]])
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["records_skipped"], 1)
        self.assertEqual(result["data"]["batches_pushed"], 1)

    def test_held_row_with_nul_persists(self):
        result = _run([[GOOD, dict(BAD, ln="Ban\x00da")]])
        self.assertTrue(result["success"])
        held = ETLHeldRecord.objects.get(external_id="2")
        self.assertEqual(held.raw_payload["ln"], "Banda")

    def test_failed_persistence_is_retried_with_the_next_batch(self):
        bad3 = dict(BAD, id="3")
        real = ETLService._upsert_held
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db blip")
            return real(*args, **kwargs)

        with mock.patch.object(ETLService, "_upsert_held", side_effect=flaky):
            with self.assertLogs("api_etl.services.base", level="ERROR"):
                result = _run([[BAD], [bad3]])
        self.assertTrue(result["success"])
        self.assertEqual(
            sorted(ETLHeldRecord.objects.values_list("external_id", flat=True)), ["2", "3"])

    def test_failed_push_does_not_resolve_held_records(self):
        _run([[BAD]])

        class FailingSink:
            def push(self, batch, identifier):
                return False

        service = ETLService(_Source([[dict(BAD, d="1950-12-10")]]), _adapter(), FailingSink())
        service.execute()
        self.assertIsNone(ETLHeldRecord.objects.get(external_id="2").resolved_at)

    def test_adapter_without_held_rows_is_fine(self):
        class Plain:
            def transform(self, data):
                return list(data)

        result = _run([[{"external_id": "1"}]], adapter=Plain())
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["records_held_open"], 0)
        self.assertEqual(ETLHeldRecord.objects.count(), 0)


class HeldRecordsCommandTestCase(TestCase):

    def _make(self, source, ext, reasons, resolved=False):
        from django.utils import timezone
        now = timezone.now()
        ETLHeldRecord.objects.create(
            source_name=source, external_id=ext, reasons=reasons, raw_payload={},
            first_seen=now, last_seen=now, resolved_at=now if resolved else None)

    def _out(self, *args):
        out = StringIO()
        call_command("etl_held_records", *args, stdout=out)
        return out.getvalue()

    def test_open_only_by_default_with_counts_per_reason(self):
        self._make("a", "1", ["dob"])
        self._make("a", "2", ["dob", "last_name"])
        self._make("a", "3", ["dob"], resolved=True)
        self._make("b", "4", ["first_name"])
        out = self._out()
        self.assertIn("a: 2 open", out)
        self.assertIn("  dob: 2", out)
        self.assertIn("  last_name: 1", out)
        self.assertIn("b: 1 open", out)

    def test_all_and_source_filter(self):
        self._make("a", "1", ["dob"])
        self._make("a", "3", ["dob"], resolved=True)
        self._make("b", "4", ["first_name"])
        out = self._out("--all", "--source", "a")
        self.assertIn("a: 2 held (incl. resolved)", out)
        self.assertNotIn("b: 1", out)

    def test_empty(self):
        self.assertIn("No held records.", self._out())
