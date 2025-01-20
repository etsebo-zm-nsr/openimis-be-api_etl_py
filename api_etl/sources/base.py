import abc
from typing import Any, Generator


class DataSource(metaclass=abc.ABCMeta):
    """
    Represents Data Source
    Provides the data for Data Adapter
    """

    class Error(Exception):
        pass

    @abc.abstractmethod
    def pull(self) -> Generator[Any, None, None]:
        raise NotImplementedError("pull() not implemented")
