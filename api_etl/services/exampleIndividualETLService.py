from api_etl.adapters import DataAdapter, ExampleIndividualAdapter
from api_etl.services.base import ETLService
from api_etl.sinks import DataSink, IndividualImportSink
from api_etl.sources import DataSource, ExampleIndividualSource
from core.models import User
from individual.services import IndividualService


class ExampleIndividualETLService(ETLService):
    """
    ETL Pipeline for the mocked Individual API
    """

    def __init__(self,
                 user: User,
                 source: DataSource = None,
                 adapter: DataAdapter = None,
                 sink: DataSink = None):
        super().__init__(
            source=source or ExampleIndividualSource(),
            adapter=adapter or ExampleIndividualAdapter(),
            sink=sink or IndividualImportSink(user)
        )
