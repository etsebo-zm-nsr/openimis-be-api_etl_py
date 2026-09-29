"""Location resolution against a loaded gazetteer tree.

A source sends place NAMES; openIMIS matches a person's location on name AND code
together. Both therefore have to come from the loaded tree, not one from the source and
one derived from it — a derived code silently creates a second village row for a place
that already exists.
"""
from django.test import TestCase

from api_etl.config import build_source_config
from api_etl.adapters.base_mapping_adapter import BaseMappingAdapter
from api_etl.locations import LocationIndex, apply_aliases, normalise_place, place_key
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


class PlaceKeyTestCase(TestCase):
    """Space-insensitive matching. `normalise_place` maps every non-alphanumeric to a
    space, so ZISPIS and the GEO file disagree on where an apostrophe (or hyphen) landed
    and never produce the same string. `place_key` must agree regardless.

    Measured: 1,356 records unresolved for this reason alone.
    """

    def test_apostrophe_pairs_converge(self):
        pairs = [
            ("SHIWANGA'NDU", "SHIWANGANDU"),
            ("MNGO'MBA", "MNG OMBA"),
            ("CHING'OMBE", "CHINGOMBE"),
            ("NG'UNYAMA", "NGUNYAMA"),
        ]
        for zispis, geo in pairs:
            self.assertEqual(place_key(zispis), place_key(geo), f"{zispis!r} vs {geo!r}")

    def test_genuinely_different_names_stay_different(self):
        self.assertNotEqual(place_key("MWICHE"), place_key("MWICHENE"))

    def test_empty_is_none(self):
        self.assertIsNone(place_key(""))
        self.assertIsNone(place_key(None))


class LocationIndexTestCase(TestCase):

    def setUp(self):
        _tree()
        self.index = LocationIndex(depth=3)

    def test_resolves_district_constituency_ward(self):
        self.assertEqual(self.index.resolve(["ITEZHI-TEZHI", "ITEZHI-TEZHI", "MASEMU"]),
                         ("9.903.130.9", "MASEMU"))

    def test_resolution_is_spelling_tolerant(self):
        self.assertIsNotNone(self.index.resolve(["itezhi tezhi", "ITEZHI-TEZHI", " masemu "]))

    def test_resolution_is_space_insensitive(self):
        """The property place_key exists for: an apostrophe folded to a space on one
        side and dropped on the other must still resolve to the same node."""
        r = Location(code="10", type="R", name="MUCHINGA"); r.save()
        d = Location(code="10.1", type="D", name="SHIWANGANDU", parent=r); d.save()
        c = Location(code="10.1.1", type="W", name="MPIKA", parent=d); c.save()
        v = Location(code="10.1.1.1", type="V", name="MNGOMBA", parent=c); v.save()
        index = LocationIndex(depth=3)
        self.assertEqual(index.resolve(["SHIWANGA'NDU", "MPIKA", "MNGO'MBA"]),
                         ("10.1.1.1", "MNGOMBA"))

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

    def test_alias_lookup_is_space_insensitive(self):
        """An alias keyed on the GEO spelling must still match ZISPIS's apostrophe
        variant, and vice versa."""
        out = apply_aliases(["SHIWANGA'NDU", "X", "Y"],
                            {"district": {"SHIWANGANDU": "SHIWANGA NDU (CANONICAL)"}},
                            ["district", "constituency", "ward"])
        self.assertEqual(out[0], "SHIWANGA NDU (CANONICAL)")


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

    def test_unresolved_row_still_carries_a_location_code_key(self):
        """A record that fails to resolve must still carry the column (as None), or a
        batch where EVERY row fails to resolve has no location_code column at all -
        openIMIS then rejects the whole upload for a missing header instead of the rows
        individually."""
        out = self._adapter().transform([dict(self.ROW, w="NOWHERE")])
        self.assertIn("location_code", out[0])
        self.assertIsNone(out[0]["location_code"])

    def test_a_batch_where_nothing_resolves_still_produces_a_location_code_header(self):
        """End-to-end through data_to_file, which is what openIMIS actually reads."""
        from api_etl.utils import data_to_file
        adapter = self._adapter()
        out = adapter.transform([dict(self.ROW, w="NOWHERE"),
                                 dict(self.ROW, id="2", w="ALSO NOWHERE")])
        self.assertEqual(len(adapter.unresolved_locations), 2)
        file = data_to_file(out, "batch")
        file.file.seek(0)
        header = file.file.read().decode("utf-8").splitlines()[0]
        self.assertIn("location_code", header.split(","))


class LoaderCheckTestCase(TestCase):
    """`etl_load_locations`'s internal uniqueness check, now on `place_key` rather than
    `normalise_place` - see api_etl.locations.place_key."""

    def _command(self):
        from django.core.management import load_command_class
        cmd = load_command_class("api_etl", "etl_load_locations")
        cmd.widths = None
        return cmd

    LEVELS = [("R", "code", "name")]

    def test_apostrophe_spelling_of_the_same_place_is_not_a_false_clash(self):
        rows = [{"code": "1", "name": "SHIWANGA'NDU"}, {"code": "1", "name": "SHIWANGANDU"}]
        self.assertEqual(self._command()._check(rows, self.LEVELS), 0)

    def test_two_codes_colliding_only_once_spaces_are_ignored_are_flagged(self):
        """A name clash that only exists under space-insensitive matching must still be
        caught - this is exactly what the mandatory --dry-run re-check against the real
        GEO file guards against."""
        rows = [{"code": "1", "name": "MWICHE"}, {"code": "2", "name": "MWICH E"}]
        self.assertGreater(self._command()._check(rows, self.LEVELS), 0)

    def test_genuinely_distinct_names_are_not_flagged(self):
        rows = [{"code": "1", "name": "MWICHE"}, {"code": "2", "name": "MWICHENE"}]
        self.assertEqual(self._command()._check(rows, self.LEVELS), 0)


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
