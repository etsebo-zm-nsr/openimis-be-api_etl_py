import abc
from typing import Any, Iterable


class DataAdapter(metaclass=abc.ABCMeta):
    """
    Represents Data Adapter
    Allows data transformation between source and sink
    """

    class Error(Exception):
        pass

    @abc.abstractmethod
    def transform(self, data: Iterable[Any]) -> Iterable[Any]:
        """
        Transform method of data adapter should be designed with specific data source and sink.
        The contract is dependent only on those components.
        """
        raise NotImplementedError("transform() not implemented")
