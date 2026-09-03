"""Check that individual_schema declares every column the connectors emit.

`individual_schema` and a connector's `field_map` are both runtime configuration,
applied through the Django admin per deployment. That is the right place for them - but
it means the two can be configured out of step, and the failure mode is unhelpful:
`validate_dataframe_headers` rejects the ENTIRE upload, at import time, after a
successful pull, naming the offending column but not the connector it came from.

This command turns that into a pre-flight check. Run it after configuring a connector
and after any change to individual_schema.

    manage.py etl_check_schema
    manage.py etl_check_schema --source zispis
    manage.py etl_check_schema --print-missing     # JSON to paste into the admin

Exits non-zero when something is missing, so it can gate a deployment step.
"""
import json

from django.core.management.base import BaseCommand

from api_etl.config import GROUP_AGGREGATION_COLUMN
from api_etl.registry import get_etl_source, list_etl_sources

# Consumed by individual/ before schema validation, so never declared.
MAGIC_COLUMNS = {"recipient_info", GROUP_AGGREGATION_COLUMN, "individual_role"}


def emitted_columns(config):
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


class Command(BaseCommand):
    help = "Verify individual_schema declares every column the ETL connectors emit."

    def add_arguments(self, parser):
        parser.add_argument("--source", help="check only this connector")
        parser.add_argument("--print-missing", action="store_true",
                            help="print the missing properties as JSON for the admin")

    def handle(self, *args, **options):
        from individual.apps import IndividualConfig

        raw = IndividualConfig.individual_schema
        schema = (json.loads(raw) if isinstance(raw, str) else raw) or {}
        declared = set(schema.get("properties", {}))
        base = set(IndividualConfig.individual_base_fields or [])
        allowed = declared | base | MAGIC_COLUMNS

        if options["source"]:
            registration = get_etl_source(options["source"])
            if registration is None:
                self.stderr.write(self.style.ERROR(
                    f"No ETL source named {options['source']!r}. Registered: "
                    f"{', '.join(r.name for r in list_etl_sources(include_disabled=True)) or 'none'}"))
                return
            registrations = [registration]
        else:
            registrations = list_etl_sources(include_disabled=True)

        if not registrations:
            self.stdout.write(self.style.WARNING("No ETL connectors are registered."))
            return

        all_missing, failed = {}, False
        for registration in registrations:
            try:
                config = registration.build_config()
            except Exception as exc:                      # noqa: BLE001
                failed = True
                self.stdout.write(self.style.ERROR(
                    f"  {registration.name}: config could not be built - {exc}"))
                continue

            missing = sorted(emitted_columns(config) - allowed)
            if missing:
                failed = True
                all_missing.update({m: {"type": "string"} for m in missing})
                self.stdout.write(self.style.ERROR(
                    f"  {registration.name}: {len(missing)} undeclared column(s)"))
                for name in missing:
                    self.stdout.write(f"      {name}")
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"  {registration.name}: OK ({len(emitted_columns(config))} columns)"))

        if not failed:
            self.stdout.write(self.style.SUCCESS(
                "\nEvery registered connector's columns are declared."))
            return

        self.stdout.write(self.style.ERROR(
            "\nAn upload containing an undeclared column is rejected in full."
            "\nAdd the missing properties to individual_schema (Core > Module "
            "configurations > module=individual, layer=be), or remove them from the "
            "connector's field_map."))
        if options["print_missing"] and all_missing:
            self.stdout.write("\nMissing properties (types are a guess - correct them):")
            self.stdout.write(json.dumps({"properties": all_missing}, indent=2))
        raise SystemExit(1)
