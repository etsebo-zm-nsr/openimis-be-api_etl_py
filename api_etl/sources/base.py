import abc
from typing import Any, Generator, Tuple, Optional


class DataSource(metaclass=abc.ABCMeta):
    """
    Represents Data Source
    Provides the data for Data Adapter
    """

    class Error(Exception):
        pass

    @abc.abstractmethod
    def pull(self) -> Generator[Tuple[Any, Optional[Any]], None, None]:
        """
        Returns:
            A generator yielding tuples of (raw_batch, identifier - optional).
        """
        raise NotImplementedError("pull() not implemented")
