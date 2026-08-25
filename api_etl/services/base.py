import abc
import logging

from api_etl.adapters import DataAdapter
from api_etl.sinks import DataSink
from api_etl.sources import DataSource

logger = logging.getLogger(__name__)


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

    def execute(self):
        try:
            for raw_batch, batch_identifier in self.source.pull():
                transformed_batch = self.adapter.transform(raw_batch)
                self.sink.push(transformed_batch, batch_identifier)
                self.records_pulled += len(raw_batch)
                self.batches_pushed += 1
                self.on_batch_complete(batch_identifier)
        except Exception as e:
            logger.error("Error in ETL pipeline: %s", str(e), exc_info=e)
            return self._error_result(str(e))

        return self._success_result()

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
        return {
            "records_pulled": getattr(self, "records_pulled", 0),
            "batches_pushed": getattr(self, "batches_pushed", 0),
            "cursor": self.observed_cursor,
        }
