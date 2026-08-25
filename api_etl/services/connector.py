"""Base class for connector ETL services.

A connector's service should be declarative: name the source and adapter classes and
inherit everything else. If a connector ever needs to reach past this into core
internals, the SPI is wrong and should be widened deliberately rather than worked around.

    class ZispisETLService(ConnectorETLService):
        source_cls = ZispisSource
        adapter_cls = ZispisAdapter

Run lifecycle (lease -> incremental watermark -> per-batch cursor -> finish) is handled
here, so every connector gets resumable incremental sync for free.
"""
import logging

from api_etl.models import ETLSyncState
from api_etl.services.base import ETLService
from api_etl.sinks.individual_import_sink import IndividualImportSink

logger = logging.getLogger(__name__)


class ConnectorETLService(ETLService):
    source_cls = None
    adapter_cls = None
    sink_cls = IndividualImportSink

    def __init__(self, user, config, source=None, adapter=None, sink=None, watermark=None):
        if config is None:
            raise ValueError(f"{type(self).__name__} requires a SourceConfig")
        if source is None and self.source_cls is None:
            raise ValueError(f"{type(self).__name__} must define source_cls")
        if adapter is None and self.adapter_cls is None:
            raise ValueError(f"{type(self).__name__} must define adapter_cls")

        self.cfg = config
        self.user = user

        if watermark is None and config.incremental.enabled:
            watermark = ETLSyncState.get_watermark(config.name) or config.incremental.initial_cursor
        self.watermark = watermark

        super().__init__(
            source=source or self.source_cls(config, watermark=watermark),
            adapter=adapter or self.adapter_cls(config),
            sink=sink or self.sink_cls(user, config=config),
        )

    @property
    def name(self):
        return self.cfg.name

    # ---------------------------------------------------------------- lifecycle

    def execute(self, use_sync_state=True):
        """Run the pipeline under a sync-state lease.

        `use_sync_state=False` skips all state handling, for tests and one-off pulls.
        """
        if not use_sync_state:
            return super().execute()

        try:
            ETLSyncState.acquire(self.name)
        except ETLSyncState.LeaseError as exc:
            logger.warning("api_etl[%s]: %s", self.name, exc)
            return {
                "success": False,
                "message": "ETL source is already running",
                "detail": str(exc),
                "data": self._stats(),
            }

        result = super().execute()
        ETLSyncState.finish(
            self.name,
            success=bool(result.get("success")),
            error=result.get("detail"),
            stats=result.get("data"),
        )
        return result

    def on_batch_complete(self, batch_identifier):
        """Persist the cursor as soon as a batch is safely landed."""
        if not self.cfg.incremental.enabled:
            return
        ETLSyncState.advance_cursor(
            self.name,
            self.observed_cursor,
            records_pulled=self.records_pulled,
            batches_pushed=self.batches_pushed,
        )
