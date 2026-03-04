"""
Abstract base adapter for venue order book connections.

Every venue adapter must implement this interface. The adapter owns:
  - Connecting to the venue (WS or fixture replay)
  - Normalizing venue-specific data into the canonical OrderBook model
  - Emitting snapshots/deltas through a callback
  - Reconnection with backoff on disconnect
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Optional

from server.models import OrderBook, Venue

logger = logging.getLogger(__name__)

# Type for the callback the adapter fires on every book update
BookUpdateCallback = Callable[[OrderBook], Awaitable[None]]


class BaseAdapter(ABC):
    """
    Abstract base class for venue adapters.

    Subclasses must implement:
        _connect()   - establish connection and begin streaming
        _disconnect() - clean up connection resources
        venue        - property returning the Venue enum

    The base class provides:
        - start() / stop() lifecycle with reconnection loop
        - Exponential backoff on disconnect
        - Callback registration for book updates
    """

    INITIAL_BACKOFF: float = 1.0
    MAX_BACKOFF: float = 30.0
    BACKOFF_MULTIPLIER: float = 2.0

    def __init__(self, market_id: str, on_book_update: BookUpdateCallback):
        self.market_id = market_id
        self._on_book_update = on_book_update
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._backoff = self.INITIAL_BACKOFF

    @property
    @abstractmethod
    def venue(self) -> Venue:
        ...

    @abstractmethod
    async def _connect(self) -> None:
        """
        Establish connection and start receiving data.
        Should call self._emit(book) for each update.
        Must raise on fatal errors; will be retried by the run loop.
        """
        ...

    @abstractmethod
    async def _disconnect(self) -> None:
        """Clean up connection resources."""
        ...

    async def _emit(self, book: OrderBook) -> None:
        """Send a normalized book update to the registered callback."""
        try:
            await self._on_book_update(book)
            self._backoff = self.INITIAL_BACKOFF
        except Exception:
            logger.exception(f"[{self.venue.value}] Error in book update callback")

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"[{self.venue.value}] Adapter started for {self.market_id}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._disconnect()
        logger.info(f"[{self.venue.value}] Adapter stopped")

    async def _run_loop(self) -> None:
        """Main loop with exponential backoff reconnection."""
        while self._running:
            try:
                logger.info(f"[{self.venue.value}] Connecting to {self.market_id}...")
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    f"[{self.venue.value}] Connection error, "
                    f"retrying in {self._backoff:.1f}s"
                )

            if not self._running:
                break

            try:
                await asyncio.sleep(self._backoff)
            except asyncio.CancelledError:
                break

            self._backoff = min(
                self._backoff * self.BACKOFF_MULTIPLIER,
                self.MAX_BACKOFF,
            )

        await self._disconnect()