"""Tests for the shared mapping adapter.

Each assertion here corresponds to a failure mode that is SILENT in production:

* `national_id` normalisation drift -> cross-source linkage stops matching and the
  registry accumulates duplicate people. This is why normalisation lives in the core
  and not in each connector.
* blank `group_code` -> `_get_grouped_individuals()` filters the row out and the person
  imports with no household, with no error anywhere (G5).
* blank/invalid `individual_role` -> `_individual_role_parser` calls `.upper()` on a NaN
  and raises deep inside the import workflow (G6).
"""
from django.test import TestCase

from api_etl.adapters.base_mapping_adapter import BaseMappingAdapter
from api_etl.config import build_source_config


def _adapter(**adapter_cfg):
    cfg = build_source_config("test", {"adapter": adapter_cfg})
    return BaseMappingAdapter(cfg)


class ResolvePathTestCase(TestCase):

    def test_flat_key(self):
        self.assertEqual(BaseMappingAdapter.resolve_path({"a": 1}, "a"), 1)

    def test_literal_key_containing_a_slash_wins(self):
        """Kobo flattens repeat groups into literal 'household/hoh_name' keys."""
        row = {"household/hoh_name": "Banda", "household": {"hoh_name": "WRONG"}}
        self.assertEqual(BaseMappingAdapter.resolve_path(row, "household/hoh_name"), "Banda")

    def test_nested_via_slash(self):
        self.assertEqual(BaseMappingAdapter.resolve_path({"a": {"b": 2}}, "a/b"), 2)

    def test_nested_via_dot(self):
        self.assertEqual(BaseMappingAdapter.resolve_path({"a": {"b": 2}}, "a.b"), 2)

    def test_missing_path_returns_none(self):
        self.assertIsNone(BaseMappingAdapter.resolve_path({"a": {}}, "a/b/c"))

    def test_descending_into_a_scalar_returns_none(self):
        self.assertIsNone(BaseMappingAdapter.resolve_path({"a": 5}, "a/b"))

    def test_empty_inputs(self):
        self.assertIsNone(BaseMappingAdapter.resolve_path(None, "a"))
        self.assertIsNone(BaseMappingAdapter.resolve_path({"a": 1}, ""))


class NormaliseNationalIdTestCase(TestCase):
    """Must be byte-identical across every connector, forever."""

    def test_strips_punctuation_and_uppercases(self):
        self.assertEqual(BaseMappingAdapter.normalise_national_id("123-456/789"), "123456789")
        self.assertEqual(BaseMappingAdapter.normalise_national_id(" ab-12 "), "AB12")

    def test_separator_variants_converge(self):
        forms = ["123456/78/1", "123456-78-1", "123456 78 1", "123456781"]
        normalised = {BaseMappingAdapter.normalise_national_id(f) for f in forms}
        self.assertEqual(normalised, {"123456781"},
                         "separator variants must collapse to one canonical form")

    def test_case_variants_converge(self):
        self.assertEqual(BaseMappingAdapter.normalise_national_id("zm1234a"),
                         BaseMappingAdapter.normalise_national_id("ZM1234A"))

    def test_blank_and_none_become_none(self):
        for value in (None, "", "   ", "///", "--"):
            self.assertIsNone(BaseMappingAdapter.normalise_national_id(value), value)

    def test_numeric_input_is_accepted(self):
        """pandas can hand back a number rather than a string."""
        self.assertEqual(BaseMappingAdapter.normalise_national_id(100001101), "100001101")


class ParseDateTestCase(TestCase):

    def test_configured_format(self):
        self.assertEqual(_adapter().parse_date("1990-04-05"), "1990-04-05")

    def test_iso_with_time_component(self):
        adapter = _adapter()
        self.assertEqual(adapter.parse_date("2026-01-02T13:45:00Z"), "2026-01-02")

    def test_custom_format(self):
        adapter = _adapter(date_formats=["%d/%m/%Y"])
        self.assertEqual(adapter.parse_date("05/04/1990"), "1990-04-05")

    def test_unparseable_returns_none_rather_than_raising(self):
        self.assertIsNone(_adapter().parse_date("not a date"))

    def test_blank_returns_none(self):
        self.assertIsNone(_adapter().parse_date(""))
        self.assertIsNone(_adapter().parse_date(None))


class MapRoleTestCase(TestCase):
    """G6: the value must always be a valid GroupIndividual.Role attribute name."""

    def test_blank_falls_back_to_default(self):
        adapter = _adapter(role_field="rel")
        self.assertEqual(adapter.map_role(None), "OTHER_RELATIVE")
        self.assertEqual(adapter.map_role(""), "OTHER_RELATIVE")

    def test_unknown_role_falls_back_rather_than_emitting_garbage(self):
        self.assertEqual(_adapter().map_role("chief cook"), "OTHER_RELATIVE")

    def test_role_map_translates_source_vocabulary(self):
        adapter = _adapter(role_map={"1": "HEAD", "2": "SPOUSE"})
        self.assertEqual(adapter.map_role("1"), "HEAD")
        self.assertEqual(adapter.map_role("2"), "SPOUSE")

    def test_direct_role_name_passes_through(self):
        self.assertEqual(_adapter().map_role("HEAD"), "HEAD")

    def test_case_and_separator_insensitive(self):
        self.assertEqual(_adapter().map_role("other relative"), "OTHER_RELATIVE")
        self.assertEqual(_adapter().map_role("other-relative"), "OTHER_RELATIVE")

    def test_custom_default_role_is_honoured(self):
        self.assertEqual(_adapter(default_role="NOT_RELATED").map_role("???"), "NOT_RELATED")

    def test_every_emitted_role_exists_on_the_model(self):
        from individual.models import GroupIndividual
        adapter = _adapter(role_map={"h": "HEAD", "x": "nonsense"})
        for raw in ("h", "x", "", None, "SPOUSE", "weird value"):
            self.assertIsNotNone(getattr(GroupIndividual.Role, adapter.map_role(raw), None))


class MapRecipientTestCase(TestCase):

    def test_primary_synonyms(self):
        for value in ("1", "primary", "true", "yes", "head", "HEAD", " Primary "):
            self.assertEqual(BaseMappingAdapter.map_recipient(value), "1", value)

    def test_secondary_synonyms(self):
        for value in ("2", "secondary", "SECONDARY"):
            self.assertEqual(BaseMappingAdapter.map_recipient(value), "2", value)

    def test_blank_and_unknown_are_none(self):
        for value in (None, "", "maybe", "0"):
            self.assertIsNone(BaseMappingAdapter.map_recipient(value), value)


class TransformRowTestCase(TestCase):

    BASE = dict(
        field_map={"first_name": "firstName", "last_name": "lastName", "dob": "dateOfBirth"},
        external_id_field="id",
        external_id_prefix="zispis:",
        group_code_field="householdId",
        group_code_prefix="zispis:hh:",
        role_field="relationship",
        role_map={"head": "HEAD", "spouse": "SPOUSE"},
        recipient_field="isRecipient",
        national_id_field="nrc",
    )

    ROW = {
        "id": 88213,
        "firstName": "Chanda",
        "lastName": "Banda",
        "dateOfBirth": "1990-04-05",
        "householdId": "HH-1",
        "relationship": "head",
        "isRecipient": "1",
        "nrc": "123456/78/1",
    }

    def test_full_row(self):
        record = _adapter(**self.BASE).transform_row(self.ROW)
        self.assertEqual(record["first_name"], "Chanda")
        self.assertEqual(record["dob"], "1990-04-05")
        self.assertEqual(record["external_id"], "zispis:88213")
        self.assertEqual(record["group_code"], "zispis:hh:HH-1")
        self.assertEqual(record["individual_role"], "HEAD")
        self.assertEqual(record["recipient_info"], "1")
        self.assertEqual(record["national_id"], "123456781")

    def test_household_ref_mirrors_group_code(self):
        """group_code is stripped by _clean_json_ext() after grouping; this keeps a copy."""
        record = _adapter(**self.BASE).transform_row(self.ROW)
        self.assertEqual(record["household_ref"], record["group_code"])

    def test_ids_are_namespaced_so_two_sources_cannot_collide(self):
        zispis = _adapter(**self.BASE).transform_row(self.ROW)
        kobo_cfg = dict(self.BASE, external_id_prefix="kobo:", group_code_prefix="kobo:hh:")
        kobo = _adapter(**kobo_cfg).transform_row(self.ROW)
        self.assertNotEqual(zispis["external_id"], kobo["external_id"])
        self.assertNotEqual(zispis["group_code"], kobo["group_code"])

    def test_row_without_external_id_is_skipped(self):
        row = dict(self.ROW)
        del row["id"]
        self.assertIsNone(_adapter(**self.BASE).transform_row(row))

    def test_missing_group_code_omits_it_rather_than_emitting_blank(self):
        """G5: a blank group_code is silently dropped by household formation."""
        row = dict(self.ROW, householdId=None)
        record = _adapter(**self.BASE).transform_row(row)
        self.assertIsNotNone(record, "row should still import, just without a household")
        self.assertNotIn("group_code", record)
        self.assertNotIn("household_ref", record)

    def test_role_is_never_blank_when_a_household_is_present(self):
        """G6: .upper() on a NaN raises inside the import workflow."""
        row = dict(self.ROW, relationship=None)
        record = _adapter(**self.BASE).transform_row(row)
        self.assertTrue(record["individual_role"])
        self.assertEqual(record["individual_role"], "OTHER_RELATIVE")

    def test_constants_are_applied(self):
        adapter = _adapter(**dict(self.BASE, constants={"location_code": "ZM-01"}))
        self.assertEqual(adapter.transform_row(self.ROW)["location_code"], "ZM-01")

    def test_national_id_is_normalised_not_passed_through(self):
        record = _adapter(**self.BASE).transform_row(dict(self.ROW, nrc=" 123456-78-1 "))
        self.assertEqual(record["national_id"], "123456781")

    def test_provenance_label_is_stamped(self):
        cfg = build_source_config("test", {
            "adapter": self.BASE,
            "provenance": {"data_source_label": "ZISPIS"},
        })
        record = BaseMappingAdapter(cfg).transform_row(self.ROW)
        self.assertEqual(record["beneficiary_data_source"], "ZISPIS")

    def test_two_connectors_normalise_the_same_id_identically(self):
        """The property cross-source linkage depends on."""
        zispis = _adapter(**self.BASE).transform_row(dict(self.ROW, nrc="123456/78/1"))
        kobo_cfg = dict(self.BASE, external_id_prefix="kobo:", national_id_field="nrc")
        kobo = _adapter(**kobo_cfg).transform_row(dict(self.ROW, nrc="123456-78-1"))
        self.assertEqual(zispis["national_id"], kobo["national_id"])


class TransformTestCase(TestCase):

    def test_none_input_raises(self):
        with self.assertRaises(BaseMappingAdapter.Error):
            _adapter().transform(None)

    def test_skipped_rows_are_dropped_from_the_batch(self):
        adapter = _adapter(external_id_field="id")
        out = adapter.transform([{"id": 1}, {"no_id": 2}, {"id": 3}])
        self.assertEqual([r["external_id"] for r in out], ["1", "3"])

    def test_empty_batch(self):
        self.assertEqual(list(_adapter().transform([])), [])
