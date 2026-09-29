"""Identifier-column float coercion, exercised against the real database.

`IndividualImportService.import_loaders` reads our CSV with
`pd.read_csv(f, dtype={"location_code": str})` - only location_code is protected, so a
purely numeric national_id comes back as a float. With maker-checker OFF (the default),
`import_individuals` creates the Individual rows SYNCHRONOUSLY, before
`_coerce_identifier_columns` ever runs - so both the staged `IndividualDataSource` row
and the `Individual` it produced need correcting, and the correct value has to come from
the original batch record rather than from reformatting the float (which loses a leading
zero).
"""
from django.test import TestCase

from api_etl.sinks.individual_import_sink import IndividualImportSink, _as_identifier_string
from core.models import Language
from core.test_helpers import LogInHelper
from individual.models import Individual, IndividualDataSource, IndividualDataSourceUpload


class CoerceIdentifierColumnsTestCase(TestCase):

    def setUp(self):
        Language.objects.get_or_create(code='en', defaults={'name': 'English', 'sort_order': 1})
        self.user = LogInHelper().get_or_create_user_api()
        self.sink = IndividualImportSink(self.user)

    def _upload(self):
        upload = IndividualDataSourceUpload(source_name="t", source_type="individual import")
        upload.save(username=self.user.login_name)
        return upload

    def _result(self, upload):
        return {"success": True, "data": {"upload_uuid": upload.uuid}}

    def _row(self, upload, individual=None, national_id=12345678.0, external_id="zispis:1"):
        row = IndividualDataSource(
            upload=upload, individual=individual,
            json_ext={"external_id": external_id, "national_id": national_id},
            validations={}, user_created=self.user, user_updated=self.user,
        )
        row.save(username=self.user.login_name)
        return row

    def test_staged_row_national_id_is_restored_from_the_original_batch(self):
        """The original batch record, not a reformatted float, so a leading zero
        ("012345678") survives - reformatting it would produce "12345678"."""
        upload = self._upload()
        row = self._row(upload)
        batch = [{"external_id": "zispis:1", "national_id": "012345678"}]

        self.sink._coerce_identifier_columns(self._result(upload), batch)

        row.refresh_from_db()
        self.assertEqual(row.json_ext["national_id"], "012345678")

    def test_reformatting_a_float_loses_a_leading_zero(self):
        """The fallback path (no matching batch record) reformats instead - documenting
        exactly the loss the batch-matching path in test above exists to avoid."""
        self.assertEqual(_as_identifier_string(12345678.0), "12345678")

    def test_individual_created_by_the_same_upload_is_also_corrected(self):
        """Maker-checker OFF: the Individual already exists (created synchronously by
        import_individuals, from the still-float CSV) by the time this runs."""
        upload = self._upload()
        individual = Individual(first_name="A", last_name="B", dob="1990-01-01",
                                json_ext={"external_id": "zispis:1", "national_id": 12345678.0})
        individual.save(user=self.user)
        self._row(upload, individual=individual)
        batch = [{"external_id": "zispis:1", "national_id": "012345678"}]

        self.sink._coerce_identifier_columns(self._result(upload), batch)

        individual.refresh_from_db()
        self.assertEqual(individual.json_ext["national_id"], "012345678")

    def test_keys_stripped_from_the_individual_are_not_put_back(self):
        """openIMIS removes group_code / recipient_info / individual_role from an Individual
        once its household is formed. Correcting the NRC must not restore them."""
        upload = self._upload()
        individual = Individual(first_name="A", last_name="B", dob="1990-01-01",
                                json_ext={"external_id": "zispis:1", "national_id": 12345678.0})
        individual.save(user=self.user)
        self._row(upload, individual=individual)
        batch = [{"external_id": "zispis:1", "national_id": "012345678",
                  "group_code": "zm-hh-zispis:1", "recipient_info": "1", "individual_role": "HEAD"}]

        self.sink._coerce_identifier_columns(self._result(upload), batch)

        individual.refresh_from_db()
        self.assertEqual(individual.json_ext["national_id"], "012345678")
        for stripped in ("group_code", "recipient_info", "individual_role"):
            self.assertNotIn(stripped, individual.json_ext)

    def test_individual_update_uses_update_not_save(self):
        """.save() on a HistoryModel bumps the version and requires a user - neither
        belongs in a background correction of a value the import already committed."""
        upload = self._upload()
        individual = Individual(first_name="A", last_name="B", dob="1990-01-01",
                                json_ext={"external_id": "zispis:1", "national_id": 12345678.0})
        individual.save(user=self.user)
        version_before = individual.version
        self._row(upload, individual=individual)
        batch = [{"external_id": "zispis:1", "national_id": "012345678"}]

        self.sink._coerce_identifier_columns(self._result(upload), batch)

        individual.refresh_from_db()
        self.assertEqual(individual.version, version_before)

    def test_staged_row_is_still_corrected_with_no_linked_individual(self):
        """The maker-checker-ON path: no Individual exists yet, only the staged row."""
        upload = self._upload()
        row = self._row(upload)
        batch = [{"external_id": "zispis:1", "national_id": "012345678"}]

        self.sink._coerce_identifier_columns(self._result(upload), batch)

        row.refresh_from_db()
        self.assertEqual(row.json_ext["national_id"], "012345678")
        self.assertIsNone(row.individual_id)

    def test_no_matching_batch_record_falls_back_to_reformatting(self):
        """A row this sink cannot match back to a batch record (e.g. a differently
        namespaced external_id) still gets corrected, just via the fallback."""
        upload = self._upload()
        row = self._row(upload, external_id="kobo:9")

        self.sink._coerce_identifier_columns(self._result(upload), [])

        row.refresh_from_db()
        self.assertEqual(row.json_ext["national_id"], "12345678")
