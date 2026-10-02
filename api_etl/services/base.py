import abc
import logging
import math

from django.db import transaction
from django.utils import timezone

from api_etl.adapters import DataAdapter
from api_etl.sinks import DataSink
from api_etl.sources import DataSource

logger = logging.getLogger(__name__)


def sanitise_for_jsonb(value):
    """Make a source row storable in a PostgreSQL JSONB column.

    JSONB rejects lone surrogates (real data contains e.g. "\\ud819") and NaN/Infinity
    (pandas-backed sources produce them), and the JSON encoder cannot serialise arbitrary
    objects. Strings (keys and values) get surrogates replaced with U+FFFD, non-finite
    floats become None and unknown types become their str().
    """
    if isinstance(value, str):
        return "".join("\ufffd" if "\ud800" <= ch <= "\udfff" else ch for ch in value)
    if isinstance(value, dict):
        return {sanitise_for_jsonb(str(k)): sanitise_for_jsonb(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitise_for_jsonb(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return sanitise_for_jsonb(str(value))


class ETLService(metaclass=abc.ABCMeta):
    """
    ETL Service class representing a full ETL pipeline
    """

    def __init__(self,
                 source: DataSource,
                 adapter: DataAdapter,
                 sink: DataSink):
        self.source = source
        self.adapter = adapter
        self.sink = sink
        self.records_pulled = 0
        self.batches_pushed = 0
        # How many of adapter.held_rows earlier batches already persisted.
        self._held_persisted = 0

    def execute(self):
        try:
            for raw_batch, batch_identifier in self.source.pull():
                transformed_batch = self.adapter.transform(raw_batch)
                self.sink.push(transformed_batch, batch_identifier)
                self._record_held_and_resolved(transformed_batch)
                self.records_pulled += len(raw_batch)
                self.batches_pushed += 1
                self.on_batch_complete(batch_identifier)
        except Exception as e:
            logger.error("Error in ETL pipeline: %s", str(e), exc_info=e)
            return self._error_result(str(e))

        return self._success_result()

    def _record_held_and_resolved(self, transformed_batch):
        """Persist this batch's held-back records and resolve ones that now import.

        Never raises: a failure here must not abort a sync whose data has already been
        pushed. The held records are still counted in `records_skipped` either way.
        """
        adapter = getattr(self, "adapter", None)
        held_rows = getattr(adapter, "held_rows", None)
        cfg = getattr(adapter, "cfg", None)
        if held_rows is None or cfg is None:
            return
        # Imported lazily: the models module needs the app registry to be ready.
        from api_etl.models import ETLHeldRecord

        start = getattr(self, "_held_persisted", 0)
        new_rows = list(held_rows[start:])
        self._held_persisted = len(held_rows)
        source = cfg.name
        now = timezone.now()
        try:
            with transaction.atomic():
                if new_rows:
                    self._upsert_held(ETLHeldRecord, source, new_rows, now)
                imported = {str(r.get("external_id")) for r in (transformed_batch or [])
                            if isinstance(r, dict) and r.get("external_id") is not None}
                if imported:
                    ETLHeldRecord.objects.filter(
                        source_name=source, external_id__in=imported, resolved_at__isnull=True,
                    ).update(resolved_at=now)
        except Exception as e:
            logger.error("api_etl[%s]: could not persist held records: %s", source, e,
                         exc_info=e)

    @staticmethod
    def _upsert_held(model, source, rows, now):
        # Last sighting wins if one batch holds the same external_id twice.
        latest = {}
        for external_id, missing, raw in rows:
            if external_id is None:
                continue
            latest[str(external_id)] = (list(missing), sanitise_for_jsonb(raw))
        existing = {h.external_id: h for h in model.objects.filter(
            source_name=source, external_id__in=list(latest))}
        to_create, to_update = [], []
        for external_id, (reasons, payload) in latest.items():
            held = existing.get(external_id)
            if held is None:
                to_create.append(model(
                    source_name=source, external_id=external_id, reasons=reasons,
                    raw_payload=payload, first_seen=now, last_seen=now, times_seen=1))
            else:
                held.reasons = reasons
                held.raw_payload = payload
                held.last_seen = now
                held.times_seen += 1
                held.resolved_at = None
                to_update.append(held)
        if to_create:
            model.objects.bulk_create(to_create)
        if to_update:
            model.objects.bulk_update(
                to_update, ["reasons", "raw_payload", "last_seen", "times_seen", "resolved_at"])

    def on_batch_complete(self, batch_identifier):
        """Hook called after a batch has been pushed successfully.

        Cursor persistence belongs here rather than at the end of the run, so a sync
        that dies on page 30 of 40 resumes at page 30. Safe because sources sort
        ascending and `external_id` upserts are idempotent.
        """

    @property
    def observed_cursor(self):
        return getattr(getattr(self, "source", None), "observed_cursor", None)

    def _error_result(self, detail):
        # Upstream returned the summary under the key "D" while gql_mutations reads
        # result['message'], so every failure raised a KeyError instead of reporting
        # its actual cause.
        return {
            "success": False,
            "message": "Failed to execute ETL pipeline",
            "detail": detail,
            "data": self._stats(),
        }

    def _success_result(self):
        return {"success": True, "message": None, "detail": None, "data": self._stats()}

    def _stats(self):
        # getattr defaults: a subclass that overrides __init__ without calling super()
        # must still be able to report a failure rather than raising AttributeError
        # while building the error result.
        stats = {
            "records_pulled": getattr(self, "records_pulled", 0),
            "batches_pushed": getattr(self, "batches_pushed", 0),
            "cursor": self.observed_cursor,
            # Records the adapter could not represent (a required value was absent).
            # Reported rather than merely logged: they were pulled but are NOT in the
            # registry, and nothing else in the run's numbers would reveal the gap.
            "records_skipped": len(getattr(getattr(self, "adapter", None), "skipped", ())),
            # Rows whose place names did not match the loaded location tree. They are
            # imported WITHOUT a location (flagged location_match=unresolved), no longer
            # rejected; this count says how many need their location fixed up.
            "locations_unresolved": len(
                getattr(getattr(self, "adapter", None), "unresolved_locations", ())),
        }
        stats["records_held_open"] = self._held_open_count()
        return stats

    def _held_open_count(self):
        cfg = getattr(getattr(self, "adapter", None), "cfg", None)
        if cfg is None or not hasattr(self.adapter, "held_rows"):
            return 0
        try:
            from api_etl.models import ETLHeldRecord
            return ETLHeldRecord.objects.filter(
                source_name=cfg.name, resolved_at__isnull=True).count()
        except Exception as e:
            logger.error("api_etl[%s]: could not count open held records: %s", cfg.name, e)
            return 0
