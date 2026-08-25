import logging

from api_etl.sinks import DataSink
from api_etl.utils import data_to_file
from core.models import User
from individual.models import Individual
from individual.services import IndividualImportService
from workflow.services import WorkflowService
from api_etl.apps import ApiEtlConfig

logger = logging.getLogger(__name__)

IMPORT_NEW_INDIVIDUALS = "Python Import Individuals"
UPDATE_EXISTING_INDIVIDUALS = "Python Update Individuals"
WORKFLOW_GROUP = "individual"

# Upstream hardcodes this at module level. None is not itself the bug -
# BaseGroupColumnAggregationClass.set_group_aggregation_column(None) falls back to
# 'group_code' - but it left connectors no way to pass a value, and no adapter emitted a
# group_code column, so _get_grouped_individuals() filtered every row out and households
# were never formed. Connectors now supply it via config, pinned to 'group_code'
# (see api_etl.config.GROUP_AGGREGATION_COLUMN).
GROUP_AGGREGATION_COLUMN = None

# Columns that must stay strings for identity matching to work. See
# IndividualImportSink._coerce_identifier_columns.
IDENTIFIER_COLUMNS = (
    "national_id", "national_id_type", "external_id", "source_batch_id",
    "group_code", "household_ref", "recipient_info", "individual_role",
)


def _normalise_national_id(value):
    """Same canonical form the adapters emit.

    Imported from the adapter base so the sink and every connector cannot drift - if
    these two normalisations disagreed, linkage would silently stop matching.
    """
    from api_etl.adapters.base_mapping_adapter import BaseMappingAdapter
    return BaseMappingAdapter.normalise_national_id(value)


def _merge_alt_ids(match, record):
    """Carry both sources' identifiers on the linked individual.

    After linking, the next sync from EITHER source matches at stage 1 on external_id
    and never re-enters the (more expensive, more fallible) linkage path.
    """
    json_ext = match.get("json_ext") or {}
    existing = json_ext.get("alt_external_ids") or ""
    ids = {part for part in str(existing).split(",") if part}
    if json_ext.get("external_id"):
        ids.add(str(json_ext["external_id"]))
    if record.get("external_id"):
        ids.add(str(record["external_id"]))
    return ",".join(sorted(ids))


def _as_identifier_string(value):
    """Render a value as the string it was before pandas inferred a dtype for it."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value:          # NaN
            return None
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, int):
        return str(value)
    return value


class IndividualImportSink(DataSink):

    def __init__(self, user: User, config=None):
        super().__init__()
        self.user = user
        self.cfg = config
        self.service = IndividualImportService(user)
        sink_cfg = getattr(config, "sink", None)
        self.import_new_workflow = self.get_workflow(
            sink_cfg.import_workflow if sink_cfg else IMPORT_NEW_INDIVIDUALS,
            sink_cfg.workflow_group if sink_cfg else WORKFLOW_GROUP,
        )
        self.update_existing_workflow = self.get_workflow(
            sink_cfg.update_workflow if sink_cfg else UPDATE_EXISTING_INDIVIDUALS,
            sink_cfg.workflow_group if sink_cfg else WORKFLOW_GROUP,
        )

    # ---------------------------------------------------------------- config
    # Each falls back to the process-global ApiEtlConfig when no per-source config was
    # given, so the shipped ExampleIndividual* pipeline is unaffected.

    @property
    def lookup_field(self):
        return self.cfg.sink.lookup_field if self.cfg else ApiEtlConfig.sink_model_lookup_field

    @property
    def update_existing(self):
        return self.cfg.sink.update_existing if self.cfg else ApiEtlConfig.sink_update_existing

    @property
    def group_aggregation_column(self):
        return self.cfg.group_aggregation_column if self.cfg else GROUP_AGGREGATION_COLUMN

    # ---------------------------------------------------------------- push

    def push(self, data: list[dict], batch_identifier=None):
        data = self._stamp_provenance(data, batch_identifier)
        existing_records, new_records = self._split_existing_and_new(data)

        if new_records:
            self._import_new_records(new_records, batch_identifier)
        else:
            logger.debug("No new record to import")

        if existing_records:
            if self.update_existing:
                self._update_existing_records(existing_records, batch_identifier)
            else:
                logger.debug(f"Skipped updating {len(existing_records)} existing records due to "
                             f"sink.update_existing = False")
        else:
            logger.debug("No existing record to update")

    def _stamp_provenance(self, data, batch_identifier):
        """Stamp the batch id on every record.

        The adapter cannot do this - it never sees the batch identifier - but it makes an
        individual traceable back to its upload without a join.
        """
        if not batch_identifier:
            return data
        for record in data:
            record.setdefault("source_batch_id", str(batch_identifier))
        return data

    def _import_new_records(self, new_records, batch_identifier):
        import_file = data_to_file(new_records, f'import_{batch_identifier}')
        result_new = self.service.import_individuals(
            import_file, self.import_new_workflow, self.group_aggregation_column
        )
        self._tag_upload(result_new)
        self._coerce_identifier_columns(result_new)
        logger.debug(f"Imported {len(new_records)} new records with {result_new}")
        return result_new

    def _update_existing_records(self, existing_records, batch_identifier):
        update_file = data_to_file(existing_records, f'update_{batch_identifier}')
        result_existing = self.service.import_individuals(
            update_file, self.update_existing_workflow, self.group_aggregation_column
        )
        self._tag_upload(result_existing)
        self._coerce_identifier_columns(result_existing)
        logger.debug(f"Updated {len(existing_records)} existing records with {result_existing}")
        return result_existing

    def _coerce_identifier_columns(self, result):
        """Restore identifier columns to strings after pandas type inference.

        `IndividualImportService.import_loaders` reads our CSV with
        `pd.read_csv(f, dtype={"location_code": str})` - only location_code is protected.
        A purely numeric national_id therefore lands in IndividualDataSource.json_ext as
        a float (float rather than int because blank values introduce NaN), so
        "100001101" is stored as 100001101.0.

        That silently breaks cross-source linkage, which matches on the normalised
        string. Individuals are not created until the import is approved, so correcting
        the staged rows here means the Individual records inherit the correct values.
        """
        if not result or not result.get("success"):
            return
        upload_uuid = (result.get("data") or {}).get("upload_uuid")
        if not upload_uuid:
            return
        try:
            from individual.models import IndividualDataSource
            rows = IndividualDataSource.objects.filter(upload_id=upload_uuid)
            for row in rows:
                json_ext = row.json_ext or {}
                changed = False
                for column in IDENTIFIER_COLUMNS:
                    value = json_ext.get(column)
                    coerced = _as_identifier_string(value)
                    if coerced is not value and coerced != value:
                        json_ext[column] = coerced
                        changed = True
                if changed:
                    row.json_ext = json_ext
                    row.save(update_fields=["json_ext"])
        except Exception as exc:
            logger.warning("api_etl: could not coerce identifier columns for upload %s: %s",
                           upload_uuid, exc)

    def _tag_upload(self, result):
        """Correct IndividualDataSourceUpload.source_type after the fact.

        IndividualImportService._create_upload_entry hardcodes 'individual import' for
        every upload. Post-correcting here records which connector produced a batch
        without having to fork the individual module.
        """
        source_type = getattr(getattr(self.cfg, "provenance", None), "source_type", None)
        if not source_type or not result or not result.get("success"):
            return
        upload_uuid = (result.get("data") or {}).get("upload_uuid")
        if not upload_uuid:
            return
        try:
            from individual.models import IndividualDataSourceUpload
            upload = IndividualDataSourceUpload.objects.filter(uuid=upload_uuid).first()
            if upload:
                upload.source_type = source_type
                upload.save(username=self.user.login_name)
        except Exception as exc:      # provenance must never fail an import
            logger.warning("api_etl: could not tag upload %s with source_type: %s",
                           upload_uuid, exc)

    # ---------------------------------------------------------------- split

    def _split_existing_and_new(self, data: list[dict]) -> tuple[list[dict], list[dict]]:
        """Three-way split: UPDATE by external id, LINKED by national id, else NEW.

        Stage 1 - exact match on the namespaced external_id: the same record seen again
        from the same source. Straightforward update.

        Stage 2 - record linkage. A person registered fresh in Kobo may already exist
        from ZISPIS; their external_ids differ ("kobo:..." vs "zispis:..."), so stage 1
        cannot see it and a naive sink would create a duplicate human being.

        Neither upstream mechanism helps here:
          * `"uniqueness": true` compares `dataframe[field].duplicated()` - within the
            uploaded file only, never against the database.
          * DeduplicationIndividualValidationStrategy does query the DB, but
            `individual/services.py::_handle_validation_calculation` never passes the
            `incoming_data` kwarg the strategy dereferences, so it raises TypeError.

        So linkage is done here, on the normalised national_id, with a secondary field
        (DOB or surname) required before adopting an existing identity. Ambiguous cases
        are imported as NEW and flagged for a human rather than silently merged.
        """
        model_lookup_field = self.lookup_field

        data_ids = [self._get_data_id(record, model_lookup_field) for record in data]
        existing_data_id_to_db_id_map = self._get_existing_individual_ids(data_ids, model_lookup_field)

        existing_records = []
        unmatched = []
        for record in data:
            data_id = self._get_data_id(record, model_lookup_field)
            if data_id in existing_data_id_to_db_id_map:
                record['ID'] = existing_data_id_to_db_id_map[data_id]
                existing_records.append(record)
            else:
                unmatched.append(record)

        linked, new_records = self._link_by_national_id(unmatched)
        existing_records.extend(linked)
        return existing_records, new_records

    # ---------------------------------------------------------------- linkage

    def _link_by_national_id(self, records: list[dict]) -> tuple[list[dict], list[dict]]:
        if not records or not self._link_enabled:
            return [], records

        candidates = {
            _normalise_national_id(r.get("national_id"))
            for r in records if r.get("national_id")
        }
        candidates.discard(None)
        if not candidates:
            return [], records

        matches = self._find_by_national_id(candidates)

        linked, new_records = [], []
        for record in records:
            national_id = _normalise_national_id(record.get("national_id"))
            found = matches.get(national_id) if national_id else None

            if not found:
                new_records.append(record)
                continue

            if len(found) > 1:
                # Ambiguous: the same national id already appears on several people.
                # Never guess - import as new and leave it for a checker.
                record["linkage_candidate_id"] = ",".join(str(m["id"]) for m in found[:5])
                record["linkage_note"] = (
                    f"national_id matches {len(found)} existing individuals; "
                    f"imported as new pending review"
                )
                new_records.append(record)
                continue

            match = found[0]
            if self._secondary_matches(record, match):
                record["ID"] = match["id"]
                record["alt_external_ids"] = _merge_alt_ids(match, record)
                linked.append(record)
            else:
                record["linkage_candidate_id"] = str(match["id"])
                record["linkage_note"] = (
                    "national_id matched but neither dob nor last_name agreed; "
                    "imported as new pending review"
                )
                new_records.append(record)

        if linked:
            logger.info("api_etl[%s]: linked %s incoming record(s) to existing individuals "
                        "by national_id", getattr(self.cfg, "name", "?"), len(linked))
        return linked, new_records

    @property
    def _link_enabled(self):
        return bool(self.cfg and self.cfg.sink.link_on_national_id)

    @property
    def _require_secondary(self):
        return not self.cfg or self.cfg.sink.link_requires_secondary_match

    def _find_by_national_id(self, national_ids) -> dict:
        """One query per batch, resolved in Python."""
        matches = {}
        rows = (Individual.objects
                .filter(json_ext__national_id__in=list(national_ids), is_deleted=False)
                .values("id", "dob", "last_name", "json_ext"))
        for row in rows:
            key = _normalise_national_id((row.get("json_ext") or {}).get("national_id"))
            if key:
                matches.setdefault(key, []).append(row)
        return matches

    def _secondary_matches(self, record, match) -> bool:
        """Guard against a transcription error in the national id.

        A single mistyped digit should not merge two different people, so require the
        DOB or the surname to agree as well.
        """
        if not self._require_secondary:
            return True

        record_dob = str(record.get("dob") or "").strip()
        match_dob = match.get("dob")
        if record_dob and match_dob and record_dob == match_dob.strftime("%Y-%m-%d"):
            return True

        record_last = str(record.get("last_name") or "").strip().upper()
        match_last = str(match.get("last_name") or "").strip().upper()
        return bool(record_last) and record_last == match_last

    def _get_existing_individual_ids(self, data_ids: list, model_lookup_field: str) -> dict:
        filter_kwargs = {f"{model_lookup_field}__in": data_ids}
        queryset = Individual.objects.filter(**filter_kwargs)

        results = queryset.values_list(model_lookup_field, 'id')
        return dict(results) if results else {}

    def _get_data_id(self, data: dict, key: str):
        # Supports any field on individual or a field on individual.json_ext
        keys = key.split('__')
        # .get rather than [] - a record legitimately lacking the id column should be
        # treated as new, not raise KeyError mid-batch.
        return data.get(keys[-1])

    @staticmethod
    def get_workflow(name, group=WORKFLOW_GROUP):
        result = WorkflowService.get_workflows(name, group)
        if not result.get('success'):
            raise DataSink.Error(f"{result.get('message')}: {result.get('details')}")
        workflows = result.get('data', {}).get('workflows')
        if not workflows:
            raise DataSink.Error(f"Workflow not found: group={group} name={name}")
        if len(workflows) > 1:
            raise DataSink.Error(f"Multiple workflows found: group={group} name={name}")
        return workflows[0]
