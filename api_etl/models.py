"""Per-source ETL run state.

Upstream `api_etl` ships no models at all, so there is nowhere to record how far a sync
got. Without that every run is a full pull, and a run that dies half way starts over.

Deliberately a plain `models.Model` rather than openIMIS's `HistoryModel`: this is
operational state, not business data. Versioned history would be noise, and
`HistoryModel.save()` requires a user, which the scheduled path does not naturally have.
"""
import logging
from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# A run still marked RUNNING after this long is assumed to have crashed (killed process,
# container restart), and its lease may be stolen.
DEFAULT_LEASE_HOURS = 2


class ETLSyncState(models.Model):
    class Status(models.TextChoices):
        IDLE = "IDLE", "Idle"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAIL = "FAIL", "Failed"

    source_name = models.CharField(max_length=64, unique=True, db_index=True)

    # Opaque high-water mark - an ISO timestamp for most sources, but never parsed by
    # generic code; compared lexically only.
    cursor = models.CharField(max_length=255, null=True, blank=True)
    last_success_cursor = models.CharField(max_length=255, null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IDLE)
    run_started = models.DateTimeField(null=True, blank=True)
    run_finished = models.DateTimeField(null=True, blank=True)

    records_pulled = models.IntegerField(default=0)
    batches_pushed = models.IntegerField(default=0)
    last_error = models.TextField(null=True, blank=True)

    json_ext = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "api_etl_sync_state"

    def __str__(self):
        return f"{self.source_name}: {self.status} @ {self.cursor}"

    class LeaseError(Exception):
        pass

    # ---------------------------------------------------------------- lease

    @classmethod
    def acquire(cls, source_name, lease_hours=DEFAULT_LEASE_HOURS):
        """Claim the right to run this source.

        The FE trigger button, the scheduler and a manual command can all fire at once
        and none of them coordinate, so the claim is taken under select_for_update.
        """
        with transaction.atomic():
            state, _ = cls.objects.select_for_update().get_or_create(source_name=source_name)
            if state.status == cls.Status.RUNNING and state.run_started:
                age = timezone.now() - state.run_started
                if age < timedelta(hours=lease_hours):
                    raise cls.LeaseError(
                        f"ETL source {source_name!r} is already running "
                        f"(started {state.run_started.isoformat()})"
                    )
                logger.warning("api_etl[%s]: stealing lease from a run started %s ago",
                               source_name, age)
            state.status = cls.Status.RUNNING
            state.run_started = timezone.now()
            state.run_finished = None
            state.last_error = None
            state.records_pulled = 0
            state.batches_pushed = 0
            state.save()
        return state

    # ---------------------------------------------------------------- cursor

    @classmethod
    def advance_cursor(cls, source_name, cursor, records_pulled=None, batches_pushed=None):
        """Persist progress after a batch has been pushed successfully.

        Advancing per batch rather than once at the end is what lets a 40-page sync that
        dies on page 30 resume at page 30. Safe because sources page in ascending cursor
        order and `external_id` upserts are idempotent.
        """
        if not cursor:
            return
        with transaction.atomic():
            state, _ = cls.objects.select_for_update().get_or_create(source_name=source_name)
            # Never move a cursor backwards: a late out-of-order batch must not undo
            # progress already recorded.
            if state.cursor is None or str(cursor) > state.cursor:
                state.cursor = str(cursor)
            if records_pulled is not None:
                state.records_pulled = records_pulled
            if batches_pushed is not None:
                state.batches_pushed = batches_pushed
            state.save()

    @classmethod
    def get_watermark(cls, source_name):
        state = cls.objects.filter(source_name=source_name).first()
        return state.cursor if state else None

    # ---------------------------------------------------------------- finish

    @classmethod
    def finish(cls, source_name, success, error=None, stats=None):
        with transaction.atomic():
            state, _ = cls.objects.select_for_update().get_or_create(source_name=source_name)
            state.status = cls.Status.SUCCESS if success else cls.Status.FAIL
            state.run_finished = timezone.now()
            state.last_error = None if success else (error or "")[:4000]
            if stats:
                state.records_pulled = stats.get("records_pulled", state.records_pulled)
                state.batches_pushed = stats.get("batches_pushed", state.batches_pushed)
            if success:
                # On failure the cursor keeps whatever the last good batch recorded, so
                # the next run resumes there instead of re-pulling everything.
                state.last_success_cursor = state.cursor
            state.save()
        return state

    @classmethod
    def reset(cls, source_name, cursor=None):
        with transaction.atomic():
            state, _ = cls.objects.select_for_update().get_or_create(source_name=source_name)
            state.cursor = cursor
            state.last_success_cursor = None
            state.status = cls.Status.IDLE
            state.last_error = None
            state.run_started = None
            state.run_finished = None
            state.save()
        return state
