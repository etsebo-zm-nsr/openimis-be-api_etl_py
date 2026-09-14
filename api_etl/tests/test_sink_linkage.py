"""Sink linkage branches, exercised against a real database.

These were the untested branches: the suite stayed green while a live code path was
deleted, twice. Every test here goes through `_split_existing_and_new`, so a missing
method or a broken query fails the suite instead of the next live run.

The rules being pinned:
  * external_id is authoritative - same source record, same person;
  * the identity key adopts an existing person, and only when it is unambiguous;
  * a shared national id NEVER merges. Zambia issues duplicate NRCs to different
    people, so it is recorded as a question for review, never acted on.
"""
from django.test import TestCase

from api_etl.config import build_source_config
from api_etl.sinks.individual_import_sink import IndividualImportSink
from core.models import Language
from core.test_helpers import LogInHelper
from individual.models import Individual


def _cfg(**sink_overrides):
    return build_source_config("t", {"sink": sink_overrides} if sink_overrides else {})


class SinkLinkageTestCase(TestCase):

    def setUp(self):
        Language.objects.get_or_create(code='en', defaults={'name': 'English', 'sort_order': 1})
        self.user = LogInHelper().get_or_create_user_api()

    def _existing(self, **json_ext):
        individual = Individual(first_name=json_ext.pop("first_name", "Mary"),
                                last_name=json_ext.pop("last_name", "Banda"),
                                dob=json_ext.pop("dob", "1985-03-12"),
                                json_ext=json_ext)
        individual.save(user=self.user)
        return individual

    def _split(self, records, cfg=None):
        sink = IndividualImportSink(self.user, cfg or _cfg())
        return sink._split_existing_and_new(records)

    # ------------------------------------------------------------ external id

    def test_same_external_id_updates_rather_than_duplicating(self):
        existing = self._existing(external_id="zispis:1")
        updated, new = self._split([{"external_id": "zispis:1", "first_name": "Mary"}])
        self.assertEqual(len(updated), 1)
        self.assertEqual(str(updated[0]["ID"]), str(existing.id))
        self.assertEqual(new, [])

    # ---------------------------------------------------------- identity key

    def test_identity_key_does_not_merge_by_default(self):
        """The default posture: detect, record, never fuse two people silently."""
        existing = self._existing(external_id="zispis:1", identity_key="abc123")
        updated, new = self._split([{"external_id": "kobo:9", "identity_key": "abc123"}])
        self.assertEqual(updated, [])
        self.assertNotIn("ID", new[0])
        self.assertEqual(new[0]["linkage_candidate_id"], str(existing.id))
        self.assertIn("pending review", new[0]["linkage_note"])

    def test_identity_key_adopts_only_when_explicitly_enabled(self):
        existing = self._existing(external_id="zispis:1", identity_key="abc123")
        updated, new = self._split([{"external_id": "kobo:9", "identity_key": "abc123"}],
                                   cfg=_cfg(link_on_identity_key=True))
        self.assertEqual(len(updated), 1)
        self.assertEqual(str(updated[0]["ID"]), str(existing.id))

    def test_ambiguous_identity_key_is_flagged_not_merged(self):
        self._existing(external_id="zispis:1", identity_key="dup")
        self._existing(external_id="zispis:2", identity_key="dup")
        updated, new = self._split([{"external_id": "kobo:9", "identity_key": "dup"}])
        self.assertEqual(updated, [])
        self.assertEqual(len(new), 1)
        self.assertNotIn("ID", new[0])
        self.assertIn("matches 2 existing", new[0]["linkage_note"])

    def test_record_without_an_identity_key_imports_as_new(self):
        self._existing(external_id="zispis:1", identity_key="abc123")
        updated, new = self._split([{"external_id": "kobo:9"}])
        self.assertEqual(updated, [])
        self.assertEqual(len(new), 1)

    def test_external_id_still_updates_even_with_linking_off(self):
        """Turning off the heuristic must not affect the source's own identifier."""
        existing = self._existing(external_id="zispis:1", identity_key="abc123")
        updated, new = self._split([{"external_id": "zispis:1", "identity_key": "abc123"}])
        self.assertEqual(len(updated), 1)
        self.assertEqual(str(updated[0]["ID"]), str(existing.id))

    # ----------------------------------------------------------- national id

    def test_shared_national_id_is_never_merged(self):
        """The rule the whole redesign exists for."""
        self._existing(external_id="zispis:1", national_id="123456781")
        updated, new = self._split(
            [{"external_id": "kobo:9", "national_id": "123456781"}])
        self.assertEqual(updated, [], "a shared NRC must not adopt an identity")
        self.assertEqual(len(new), 1)
        self.assertNotIn("ID", new[0])

    def test_shared_national_id_is_recorded_for_review(self):
        existing = self._existing(external_id="zispis:1", national_id="123456781")
        _, new = self._split([{"external_id": "kobo:9", "national_id": "123456781"}])
        self.assertIn("NOT merged", new[0]["linkage_note"])
        self.assertEqual(new[0]["linkage_candidate_id"], str(existing.id))

    def test_national_id_flagging_can_be_disabled(self):
        self._existing(external_id="zispis:1", national_id="123456781")
        _, new = self._split([{"external_id": "kobo:9", "national_id": "123456781"}],
                             cfg=_cfg(flag_national_id_matches=False))
        self.assertNotIn("linkage_note", new[0])

    def test_identity_note_is_not_overwritten_by_the_national_id_note(self):
        self._existing(external_id="zispis:1", identity_key="dup", national_id="123456781")
        self._existing(external_id="zispis:2", identity_key="dup")
        _, new = self._split([{"external_id": "kobo:9", "identity_key": "dup",
                               "national_id": "123456781"}])
        self.assertIn("identity key", new[0]["linkage_note"])

    def test_unrelated_national_id_is_left_alone(self):
        self._existing(external_id="zispis:1", national_id="111111111")
        _, new = self._split([{"external_id": "kobo:9", "national_id": "999999999"}])
        self.assertNotIn("linkage_note", new[0])
