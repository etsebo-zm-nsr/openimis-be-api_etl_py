"""Location resolution against a loaded gazetteer tree.

A source sends place NAMES; openIMIS matches a person's location on name AND code
together. Both therefore have to come from the loaded tree, not one from the source and
one derived from it — a derived code silently creates a second village row for a place
that already exists.
"""
from django.test import TestCase

from api_etl.config import build_source_config
from api_etl.adapters.base_mapping_adapter import BaseMappingAdapter
from api_etl.locations import LocationIndex, apply_aliases, normalise_place
from location.models import Location


def _tree():
    """SOUTHERN > ITEZHI-TEZHI > ITEZHI-TEZHI > MASEMU, codes as a dotted path."""
    r = Location(code="9", type="R", name="SOUTHERN"); r.save()
    d = Location(code="9.903", type="D", name="ITEZHI-TEZHI", parent=r); d.save()
    c = Location(code="9.903.130", type="W", name="ITEZHI-TEZHI", parent=d); c.save()
    v = Location(code="9.903.130.9", type="V", name="MASEMU", parent=c); v.save()
    return v


class NormalisePlaceTestCase(TestCase):

    def test_punctuation_and_spacing_variants_agree(self):
        self.assertEqual(normalise_place("SINJEMBELA(SHANGOMBO)"),
                         normalise_place("SINJEMBELA (SHANGOMBO)"))
        self.assertEqual(normalise_place("CHAMA NORTH"), normalise_place("CHAMA-NORTH"))

    def test_case_and_whitespace(self):
        self.assertEqual(normalise_place("  itezhi-tezhi "), "ITEZHI TEZHI")

    def test_empty_is_none(self):
        self.assertIsNone(normalise_place(""))
        self.assertIsNone(normalise_place(None))


class LocationIndexTestCase(TestCase):

    def setUp(self):
        _tree()
        self.index = LocationIndex(depth=3)

    def test_resolves_district_constituency_ward(self):
        self.assertEqual(self.index.resolve(["ITEZHI-TEZHI", "ITEZHI-TEZHI", "MASEMU"]),
                         ("9.903.130.9", "MASEMU"))

    def test_resolution_is_spelling_tolerant(self):
        self.assertIsNotNone(self.index.resolve(["itezhi tezhi", "ITEZHI-TEZHI", " masemu "]))

    def test_unknown_path_is_none(self):
        self.assertIsNone(self.index.resolve(["ITEZHI-TEZHI", "ITEZHI-TEZHI", "NOWHERE"]))

    def test_incomplete_path_is_none(self):
        self.assertIsNone(self.index.resolve(["ITEZHI-TEZHI", None, "MASEMU"]))

    def test_ambiguous_path_refuses_to_guess(self):
        """Two wards reachable by one name path must resolve to NEITHER.

        Picking one arbitrarily would place people in a village they do not live in,
        which no downstream check would catch.
        """
        c2 = Location.objects.get(code="9.903.130")
        dup = Location(code="9.903.130.77", type="V", name="MASEMU", parent=c2); dup.save()
        index = LocationIndex(depth=3)
        self.assertIsNone(index.resolve(["ITEZHI-TEZHI", "ITEZHI-TEZHI", "MASEMU"]))
        self.assertEqual(len(index.ambiguous), 1)


class AliasTestCase(TestCase):

    def test_alias_rewrites_one_level(self):
        out = apply_aliases(["CHIENGI", "KAWAMBWA", "IYANGA"],
                            {"district": {"CHIENGI": "CHIENGE"}},
                            ["district", "constituency", "ward"])
        self.assertEqual(out, ["CHIENGE", "KAWAMBWA", "IYANGA"])

    def test_alias_matches_on_normalised_spelling(self):
        out = apply_aliases(["chiengi ", "X", "Y"], {"district": {"CHIENGI": "CHIENGE"}},
                            ["district", "constituency", "ward"])
        self.assertEqual(out[0], "CHIENGE")

    def test_no_aliases_is_a_passthrough(self):
        names = ["A", "B", "C"]
        self.assertEqual(apply_aliases(names, {}, ["district", "constituency", "ward"]), names)


class AdapterLocationResolutionTestCase(TestCase):

    def _adapter(self, **overrides):
        cfg = build_source_config("t", {"adapter": {
            "field_map": {"first_name": "fn", "last_name": "ln", "dob": "d",
                          "district": "dist", "constituency": "con", "ward": "w",
                          "location_name": "w"},
            "external_id_field": "id",
            "location_match_fields": ["district", "constituency", "ward"],
            **overrides}})
        return BaseMappingAdapter(cfg)

    ROW = {"id": "1", "fn": "A", "ln": "B", "d": "1990-01-01",
           "dist": "ITEZHI-TEZHI", "con": "ITEZHI-TEZHI", "w": "MASEMU"}

    def setUp(self):
        _tree()

    def test_resolved_row_carries_the_tree_code_and_name(self):
        out = self._adapter().transform([self.ROW])
        self.assertEqual(out[0]["location_code"], "9.903.130.9")
        self.assertEqual(out[0]["location_name"], "MASEMU")

    def test_unresolved_row_is_counted_not_silently_passed(self):
        adapter = self._adapter()
        adapter.transform([dict(self.ROW, w="NOWHERE")])
        self.assertEqual(len(adapter.unresolved_locations), 1)

    def test_alias_makes_an_unmatched_spelling_resolve(self):
        adapter = self._adapter(location_aliases={"ward": {"MASEMO": "MASEMU"}})
        out = adapter.transform([dict(self.ROW, w="MASEMO")])
        self.assertEqual(out[0]["location_code"], "9.903.130.9")

    def test_disabled_when_no_match_fields_configured(self):
        adapter = self._adapter(location_match_fields=[])
        out = adapter.transform([self.ROW])
        self.assertNotIn("location_code", out[0])
        self.assertEqual(adapter.unresolved_locations, [])
