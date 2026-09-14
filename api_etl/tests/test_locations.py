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


class CodePathFormatTestCase(TestCase):
    """Dotted vs fixed-width codes.

    Gazetteer codes are parent-scoped, so a node's identifier is the path from the top.
    Fixed width is what downstream systems usually want; the risk is that a code too
    long for its field shifts every level after it, which must fail loudly.
    """

    def _command(self, levels):
        from django.core.management import load_command_class
        cmd = load_command_class("api_etl", "etl_load_locations")
        cmd.widths = None
        parts = [p.split(":") for p in levels.split(",")]
        if all(len(p) == 4 for p in parts):
            cmd.widths = [int(p[3]) for p in parts]
        return cmd

    ZM = "R:province_code:province:2,D:district_code:district:4," \
         "W:constituency_code:constituency:3,V:ward_code:ward:3"

    def test_dotted_is_the_default(self):
        cmd = self._command("R:a:b,D:c:d")
        self.assertEqual(cmd._path(["9", "903"]), "9.903")

    def test_fixed_width_pads_and_concatenates(self):
        cmd = self._command(self.ZM)
        self.assertEqual(cmd._path(["9", "903", "130", "9"]), "090903130009")

    def test_fixed_width_length_is_the_sum_of_widths(self):
        cmd = self._command(self.ZM)
        self.assertEqual(len(cmd._path(["9", "903", "130", "9"])), 2 + 4 + 3 + 3)

    def test_a_childs_code_begins_with_its_parents(self):
        """So a prefix match finds a subtree, under either format."""
        cmd = self._command(self.ZM)
        parent = cmd._path(["9", "903", "130"])
        child = cmd._path(["9", "903", "130", "9"])
        self.assertTrue(child.startswith(parent))

    def test_distinct_paths_stay_distinct_when_padded(self):
        cmd = self._command(self.ZM)
        self.assertNotEqual(cmd._path(["9", "903", "13", "9"]),
                            cmd._path(["9", "903", "130", "9"]))

    def test_overlong_code_is_refused_not_truncated(self):
        """Truncating would shift every following level and relocate people."""
        from django.core.management.base import CommandError
        cmd = self._command(self.ZM)
        with self.assertRaises(CommandError) as ctx:
            cmd._path(["9", "12345"], "district")
        self.assertIn("width", str(ctx.exception))
