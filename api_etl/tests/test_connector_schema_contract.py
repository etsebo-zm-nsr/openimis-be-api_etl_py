"""Every column a connector emits must be declared in `individual_schema`.

`validate_dataframe_headers` rejects the WHOLE upload if a single emitted column is
undeclared (blocker G2), and it does so at import time, after a successful pull, with an
error that names the column but not the connector. Adding a field to a connector's
`field_map` without adding the column to `individual_schema` is therefore a change that
looks fine, passes review, and breaks the sync.

Both are runtime configuration set through the Django admin, so this checks THIS
deployment's configuration - `manage.py etl_check_schema` is the same check as an
operator-facing command.

This runs generically over every registered connector, so a connector added later gets
the check for free.

It validates the schema of THIS deployment, which is loaded into `IndividualConfig` at
startup. When the shared ETL columns are absent the test skips rather than failing, so
"this database is not set up" reports as a skip and "this deployment's connector config
and schema disagree" as a failure - two very different problems.
"""
import json

from django.test import TestCase

from api_etl.config import GROUP_AGGREGATION_COLUMN
from api_etl.registry import list_etl_sources

# Consumed by individual/ before schema validation, so never declared.
MAGIC_COLUMNS = {"recipient_info", GROUP_AGGREGATION_COLUMN, "individual_role"}

# Written by the core, not by any connector's field_map.
CORE_EMITTED = {"external_id", "household_ref", "national_id", "national_id_type",
                "beneficiary_data_source", "source_batch_id"}


def _declared_columns():
    from individual.apps import IndividualConfig
    raw = IndividualConfig.individual_schema
    if not raw:
        return set()
    schema = json.loads(raw) if isinstance(raw, str) else raw
    return set((schema or {}).get("properties", {}))


def _base_fields():
    from individual.apps import IndividualConfig
    return set(IndividualConfig.individual_base_fields or [])


def _emitted_columns(config):
    """Columns this connector's configuration will put on a row."""
    adapter = config.adapter
    emitted = set(adapter.field_map or {})
    emitted |= set(adapter.constants or {})
    emitted.add("external_id")
    if adapter.national_id_field:
        emitted.add("national_id")
    if adapter.national_id_type_field:
        emitted.add("national_id_type")
    if adapter.group_code_field:
        emitted |= {GROUP_AGGREGATION_COLUMN, "household_ref", "individual_role"}
        if adapter.recipient_field:
            emitted.add("recipient_info")
    if config.provenance.data_source_label:
        emitted.add("beneficiary_data_source")
    return emitted


class ConnectorSchemaContractTestCase(TestCase):

    def setUp(self):
        self.declared = _declared_columns()
        self.allowed = self.declared | _base_fields() | MAGIC_COLUMNS
        if not CORE_EMITTED.issubset(self.declared | MAGIC_COLUMNS):
            missing = sorted(CORE_EMITTED - self.declared)
            self.skipTest(
                "individual_schema is missing the shared ETL columns "
                f"({', '.join(missing)}), so this database is not set up for ETL. "
                "Apply the api_etl migrations and the connector configuration, then re-run."
            )

    def test_every_registered_connector_declares_its_columns(self):
        registrations = list_etl_sources(include_disabled=True)
        self.assertTrue(registrations, "no ETL connectors registered")

        problems = []
        for registration in registrations:
            try:
                config = registration.build_config()
            except Exception as exc:                      # noqa: BLE001 - reported, not raised
                problems.append(f"{registration.name}: config failed to build - {exc}")
                continue
            undeclared = sorted(_emitted_columns(config) - self.allowed)
            if undeclared:
                problems.append(
                    f"{registration.name}: emits undeclared column(s) {undeclared}. "
                    f"Add them to individual_schema (Core > Module configurations, "
                    f"module=individual), or drop them from {registration.name}'s field_map."
                )

        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_no_connector_emits_a_reserved_magic_column_via_field_map(self):
        """group_code/individual_role/recipient_info are set by the adapter itself.

        Putting one in field_map lets a source overwrite it directly - and a blank
        group_code silently drops the row from household formation (G5).
        """
        for registration in list_etl_sources(include_disabled=True):
            try:
                config = registration.build_config()
            except Exception:                             # covered by the test above
                continue
            overlap = set(config.adapter.field_map or {}) & MAGIC_COLUMNS
            self.assertEqual(
                overlap, set(),
                f"{registration.name}: field_map sets {sorted(overlap)} directly; "
                "these are derived by BaseMappingAdapter from group_code_field / "
                "role_field / recipient_field.",
            )

    def test_group_aggregation_column_is_group_code_for_every_connector(self):
        """Any other value routes to generate_unique_code(), which regenerates a random
        household code on every run and duplicates households (G5)."""
        for registration in list_etl_sources(include_disabled=True):
            try:
                config = registration.build_config()
            except Exception:
                continue
            self.assertEqual(config.group_aggregation_column, "group_code",
                             f"{registration.name} would duplicate households")
