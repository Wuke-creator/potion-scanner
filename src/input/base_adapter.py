import asyncio
from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    """Abstract base class for all signal input adapters.

    Adapters push raw signal strings onto an asyncio.Queue.
    Downstream consumers (e.g. parser) pull from the queue.
    """

    def __init__(self, queue: asyncio.Queue | None = None):
        self._queue = queue or asyncio.Queue()

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    @abstractmethod
    async def start(self) -> None:
        """Begin listening/reading signals. Called once."""

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown."""
