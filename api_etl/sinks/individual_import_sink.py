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
    "household_recipient",
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


def _numeric_schema_columns():
    """Properties `individual_schema` declares as integer or number (empty if unreadable)."""
    try:
        import json
        from individual.apps import IndividualConfig
        raw = IndividualConfig.individual_schema
        schema = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return frozenset(
            name for name, spec in (schema.get("properties") or {}).items()
            if isinstance(spec, dict) and spec.get("type") in ("integer", "number"))
    except Exception:
        return frozenset()


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
    # Each falls back to the process-global ApiEtlConfig when no per-source config is
    # given (every registered connector supplies one; this is a defensive default).

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
        """Import the batch. Returns True if every import call succeeded.

        "Succeeded" means `import_individuals` reported success AND the upload it created
        did not end in status FAIL - see `_batch_succeeded`.

        A batch with nothing to import, or whose existing records are skipped because
        `update_existing` is off, counts as success. The result is per batch, not per
        record: openIMIS reports one outcome for each of the new/update uploads.
        """
        data = self._stamp_provenance(data, batch_identifier)
        existing_records, new_records = self._split_existing_and_new(data)
        ok = True

        if new_records:
            result = self._import_new_records(new_records, batch_identifier)
            ok = self._batch_succeeded(result) and ok
        else:
            logger.debug("No new record to import")

        if existing_records:
            if self.update_existing:
                self._keep_existing_locations(existing_records)
                result = self._update_existing_records(existing_records, batch_identifier)
                ok = self._batch_succeeded(result) and ok
            else:
                logger.debug(f"Skipped updating {len(existing_records)} existing records due to "
                             f"sink.update_existing = False")
        else:
            logger.debug("No existing record to update")
        return ok

    def _batch_succeeded(self, result) -> bool:
        """True if openIMIS accepted the batch, judged on the upload row, not just `result`.

        `IndividualImportService.import_individuals` returns success even when the
        workflow fails: `_trigger_workflow` catches the error and records it as
        `IndividualDataSourceUpload.status = FAIL` instead. The returned dict alone would
        therefore report a rejected batch as imported, and the held records in it would
        be marked resolved. So the upload is read back.

        Only FAIL counts as rejection. PARTIAL_SUCCESS means the workflow ran and flagged
        some rows invalid - the batch was accepted - and the other statuses are
        in-flight or maker-checker states (staged, awaiting approval), not failures.
        If there is no upload uuid, or the row cannot be read, the reported `success`
        stands.
        """
        if not result or not result.get("success"):
            return False
        upload_uuid = (result.get("data") or {}).get("upload_uuid")
        if not upload_uuid:
            return True
        try:
            from individual.models import IndividualDataSourceUpload
            status = (IndividualDataSourceUpload.objects.filter(uuid=upload_uuid)
                      .values_list("status", flat=True).first())
        except Exception as exc:
            logger.warning("api_etl: could not read back upload %s status: %s",
                           upload_uuid, exc)
            return True
        if status == IndividualDataSourceUpload.Status.FAIL:
            logger.error("api_etl: upload %s was rejected by openIMIS (status FAIL); "
                         "treating the batch as not imported", upload_uuid)
            return False
        return True

    def _keep_existing_locations(self, existing_records):
        """Do not let an unresolved re-pull erase a location that is already set.

        An unresolved row carries `location_code`/`location_name` of None. For a NEW
        individual that is intended (imported without a location). For an EXISTING one,
        openIMIS's update SQL sets `location_id` from a LEFT JOIN on name+code, so
        None/None writes NULL and would wipe a location a data owner assigned by hand.
        Such rows are given the individual's current village (type 'V' - the only type
        the join matches) and `location_match = "unresolved_kept_existing"`;
        `source_location` is left as sent. Individuals without a village are untouched.
        """
        unresolved = [r for r in existing_records
                      if r.get("location_match") == "unresolved" and r.get("ID") is not None]
        if not unresolved:
            return
        ids = {r["ID"] for r in unresolved}
        current = {
            pk: (code, name) for pk, code, name in Individual.objects.filter(
                id__in=ids, location__isnull=False, location__type="V",
            ).values_list("id", "location__code", "location__name")
        }
        for record in unresolved:
            kept = current.get(record["ID"])
            if kept:
                record["location_code"], record["location_name"] = kept
                record["location_match"] = "unresolved_kept_existing"

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
        self._coerce_identifier_columns(result_new, new_records)
        logger.debug(f"Imported {len(new_records)} new records with {result_new}")
        return result_new

    def _update_existing_records(self, existing_records, batch_identifier):
        update_file = data_to_file(existing_records, f'update_{batch_identifier}')
        result_existing = self.service.import_individuals(
            update_file, self.update_existing_workflow, self.group_aggregation_column
        )
        self._tag_upload(result_existing)
        self._coerce_identifier_columns(result_existing, existing_records)
        logger.debug(f"Updated {len(existing_records)} existing records with {result_existing}")
        return result_existing

    def _coerce_identifier_columns(self, result, batch):
        """Restore identifier columns to strings after pandas type inference.

        `IndividualImportService.import_loaders` reads our CSV with
        `pd.read_csv(f, dtype={"location_code": str})` - only location_code is protected.
        A purely numeric national_id therefore lands in `IndividualDataSource.json_ext`
        as a float (float rather than int because blank values introduce NaN), so
        "100001101" is stored as 100001101.0.

        Two destinations need correcting, not one. With maker-checker ON, the staged
        `IndividualDataSource` rows are all there is - the `Individual` is only created
        later, on approval, and inherits the corrected value from there. With
        maker-checker OFF (the default here), `import_individuals` above creates the
        `Individual` rows synchronously, inside the `INSERT ... SELECT` that runs before
        this method is ever reached - from the still-float CSV values. So both the
        staged row (always) and, when one already exists, the `Individual` it produced
        (`IndividualDataSource.individual_id`) are corrected below.

        The corrected value comes from `batch` - the ORIGINAL record dicts the adapter
        produced, before they went through the CSV/pandas round trip - matched back to
        each staged row by `external_id` (namespaced with a source prefix, so it is
        never itself turned into a float). Reformatting the float instead
        (`_as_identifier_string`) is only a fallback for rows we cannot match back to a
        batch record: `int(float("012345678"))` is `12345678`, silently dropping the
        leading zero. Matching to the original avoids that loss entirely.
        """
        if not result or not result.get("success"):
            return
        upload_uuid = (result.get("data") or {}).get("upload_uuid")
        if not upload_uuid:
            return
        original_by_external_id = {
            record.get("external_id"): record for record in (batch or [])
            if record.get("external_id") is not None
        }
        numeric_columns = _numeric_schema_columns()
        try:
            from individual.models import IndividualDataSource, Individual
            rows = IndividualDataSource.objects.filter(upload_id=upload_uuid)
            individual_ids = set()
            for row in rows:
                json_ext, changed = self._coerce_json_ext(
                    row.json_ext, original_by_external_id, numeric_columns)
                if changed:
                    row.json_ext = json_ext
                    row.save(update_fields=["json_ext"])
                if row.individual_id:
                    individual_ids.add(row.individual_id)
            for individual in Individual.objects.filter(id__in=individual_ids):
                json_ext, changed = self._coerce_json_ext(
                    individual.json_ext, original_by_external_id, numeric_columns)
                if changed:
                    # .update(), not .save(): HistoryModel.save() bumps the row's
                    # version and requires a user - neither belongs in a background
                    # correction of a value the import already committed.
                    Individual.objects.filter(id=individual.id).update(json_ext=json_ext)
        except Exception as exc:
            logger.warning("api_etl: could not coerce identifier columns for upload %s: %s",
                           upload_uuid, exc)

    @staticmethod
    def _coerce_json_ext(json_ext, original_by_external_id, numeric_columns=frozenset()):
        """Correct IDENTIFIER_COLUMNS in one json_ext dict. Returns (json_ext, changed)."""
        json_ext = dict(json_ext or {})
        original = original_by_external_id.get(_as_identifier_string(json_ext.get("external_id")))
        changed = False
        for column in IDENTIFIER_COLUMNS:
            if column not in json_ext:
                # Correct values, never add keys. openIMIS's _clean_json_ext() removes
                # group_code, recipient_info and individual_role from each Individual once
                # the household is formed - copying them back from the batch would undo that.
                continue
            value = json_ext.get(column)
            if original is not None and column in original:
                coerced = original[column]
            else:
                coerced = _as_identifier_string(value)
            if coerced is not value and coerced != value:
                json_ext[column] = coerced
                changed = True
        if original is not None:
            changed = IndividualImportSink._restore_text_columns(
                json_ext, original, numeric_columns) or changed
        return json_ext, changed

    @staticmethod
    def _restore_text_columns(json_ext, original, numeric_columns):
        """Undo pandas' number inference on every other column. Returns True if changed.

        The same round trip that turns a national_id into a float does it to any column
        that happens to look numeric: a phone number "0977123456" comes back as the int
        977123456 (leading zero gone), or as 260969282150.0 when a blank elsewhere in the
        batch forces floats. So any value the adapter sent as text is restored from the
        original record. A column `individual_schema` declares numeric keeps its number,
        minus a float artefact (34.0 -> 34).
        """
        changed = False
        for column, value in list(json_ext.items()):
            if column in IDENTIFIER_COLUMNS or column not in original:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            sent = original[column]
            if sent is None:
                continue
            if column in numeric_columns:
                restored = int(value) if isinstance(value, float) and value.is_integer() \
                    else value
            else:
                restored = sent
            if restored is not value and (restored != value or type(restored) is not type(value)):
                json_ext[column] = restored
                changed = True
        return changed

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

        linked, new_records = self._link_by_identity_key(unmatched)
        existing_records.extend(linked)
        # Same-batch duplicates: two records that share a key with EACH OTHER, never
        # seen individually against the database. See _flag_in_batch_identity_duplicates.
        self._flag_in_batch_identity_duplicates(new_records)
        # National id never merges - it only annotates. See _flag_national_id_matches.
        self._flag_national_id_matches(new_records)
        return existing_records, new_records

    # ------------------------------------------------------- identity linkage

    def _link_by_identity_key(self, records: list[dict]) -> tuple[list[dict], list[dict]]:
        """Stage 2: match on the configured identity key.

        The key is defined once, as `adapter.identity_key_fields`, and computed by the
        adapter - so what counts as "the same person" is configuration, not logic buried
        here. Records without a complete key carry no `identity_key` and fall through to
        NEW rather than matching on a partial one.
        """
        if not records:
            return [], records

        keys = {r.get("identity_key") for r in records}
        keys.discard(None)
        if not keys:
            return [], records

        matches: dict = {}
        rows = (Individual.objects
                .filter(json_ext__identity_key__in=list(keys), is_deleted=False)
                .values("id", "json_ext"))
        for row in rows:
            key = (row.get("json_ext") or {}).get("identity_key")
            if key:
                matches.setdefault(key, []).append(row)

        linked, new_records = [], []
        for record in records:
            found = matches.get(record.get("identity_key"))
            if not found:
                new_records.append(record)
            elif len(found) > 1 or not self._identity_link_enabled:
                # Two reasons not to adopt:
                #   * several people share the key - trusting it would merge whichever
                #     came first;
                #   * linking is off, the default. The key is a heuristic, and fusing two
                #     different people is much harder to detect and undo than leaving a
                #     duplicate for review. The candidates are recorded either way, which
                #     is what a post-import deduplication step needs.
                record["linkage_candidate_id"] = ",".join(str(m["id"]) for m in found[:5])
                record["linkage_note"] = (
                    f"identity key matches {len(found)} existing individual(s); "
                    f"imported as new pending review"
                )
                new_records.append(record)
            else:
                record["ID"] = found[0]["id"]
                record["alt_external_ids"] = _merge_alt_ids(found[0], record)
                linked.append(record)

        if linked:
            logger.info("api_etl[%s]: linked %s incoming record(s) by identity key",
                        getattr(self.cfg, "name", "?"), len(linked))
        return linked, new_records

    def _flag_in_batch_identity_duplicates(self, records: list[dict]) -> None:
        """Flag identity-key collisions WITHIN this batch, never seen against the DB.

        `_link_by_identity_key` only ever queries individuals already in the database -
        two records new to the registry that share a key with EACH OTHER (a common
        shape for a first full pull, or two siblings registered together) were never
        compared and so never flagged. Detected here, on the same records, still never
        merged: an existing DB-match note (set above) is left alone, so this only fills
        in the records `_link_by_identity_key` had no evidence about.
        """
        if not records:
            return
        groups: dict = {}
        for record in records:
            key = record.get("identity_key")
            if key:
                groups.setdefault(key, []).append(record)
        flagged = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            for record in members:
                if record.get("linkage_note"):     # a DB match already explains this one
                    continue
                others = [m.get("external_id") for m in members if m is not record][:5]
                record["linkage_note"] = (
                    f"identity key shared with {len(members) - 1} other record(s) in "
                    f"this batch: {', '.join(str(o) for o in others)}"
                )
                flagged += 1
        if flagged:
            logger.info("api_etl[%s]: flagged %s record(s) sharing an identity key "
                        "with another record in the same batch", getattr(self.cfg, "name", "?"),
                        flagged)

    def _flag_national_id_matches(self, records: list[dict]) -> None:
        """Annotate national-id collisions WITHOUT merging.

        Zambia issues duplicate NRCs to different people - a known national problem, with
        deduplication still to come. Merging on a shared NRC would fuse two different
        human beings, so this only records the candidates, giving the future dedup work
        its input and a caseworker the reason a record was suspected.

        Checks the database first, then - for records still unexplained - this SAME
        batch: `_find_by_national_id` only ever sees individuals already stored, so two
        records new to the registry sharing an NRC with each other were never compared.

        Never sets record["ID"], so nothing is ever adopted on this evidence.
        """
        if not records or not self._flag_nid_enabled:
            return
        candidates = {_normalise_national_id(r.get("national_id")) for r in records}
        candidates.discard(None)
        if not candidates:
            return
        matches = self._find_by_national_id(candidates)
        batch_groups: dict = {}
        for record in records:
            national_id = _normalise_national_id(record.get("national_id"))
            if national_id:
                batch_groups.setdefault(national_id, []).append(record)
        flagged = 0
        for record in records:
            if record.get("linkage_note"):        # identity-key note takes precedence
                continue
            national_id = _normalise_national_id(record.get("national_id"))
            found = matches.get(national_id) if national_id else None
            if found:
                record["linkage_candidate_id"] = ",".join(str(m["id"]) for m in found[:5])
                record["linkage_note"] = (
                    f"national_id shared with {len(found)} existing individual(s); "
                    f"NOT merged - duplicate NRCs are issued in Zambia. For review."
                )
                flagged += 1
                continue
            members = batch_groups.get(national_id) if national_id else None
            if members and len(members) > 1:
                others = [m.get("external_id") for m in members if m is not record][:5]
                record["linkage_note"] = (
                    f"national_id shared with {len(members) - 1} other record(s) in "
                    f"this batch: {', '.join(str(o) for o in others)}; NOT merged - "
                    f"duplicate NRCs are issued in Zambia. For review."
                )
                flagged += 1
        if flagged:
            logger.info("api_etl[%s]: flagged %s record(s) sharing a national id "
                        "(not merged)", getattr(self.cfg, "name", "?"), flagged)

    # ---------------------------------------------------------------- linkage

    def _find_by_national_id(self, national_ids) -> dict:
        """Existing individuals holding any of these national ids. One query per batch.

        Used only for FLAGGING - see _flag_national_id_matches. Nothing here adopts an
        identity; duplicate NRCs are issued in Zambia and a shared id is a question, not
        an answer.
        """
        matches: dict = {}
        rows = (Individual.objects
                .filter(json_ext__national_id__in=list(national_ids), is_deleted=False)
                .values("id", "dob", "last_name", "json_ext"))
        for row in rows:
            key = _normalise_national_id((row.get("json_ext") or {}).get("national_id"))
            if key:
                matches.setdefault(key, []).append(row)
        return matches

    @property
    def _identity_link_enabled(self):
        return not self.cfg or self.cfg.sink.link_on_identity_key

    @property
    def _flag_nid_enabled(self):
        return not self.cfg or self.cfg.sink.flag_national_id_matches

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
