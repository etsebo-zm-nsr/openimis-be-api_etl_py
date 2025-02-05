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

    def execute(self):
        try:
            for raw_batch, batch_identifier in self.source.pull():
                transformed_batch = self.adapter.transform(raw_batch)
                self.sink.push(transformed_batch, batch_identifier)
        except Exception as e:
            logger.error("Error in ETL pipeline: %s", str(e), exc_info=e)
            return self._error_result(str(e))

        return self._success_result()

    @staticmethod
    def _error_result(detail):
        return {"success": False, "D": "Failed to execute ETL pipeline", "detail": detail}

    @staticmethod
    def _success_result():
        return {"success": True, "data": None}
