"""
Fixture adapter for replaying recorded JSON order book data.

Swappable with live adapters via the same BaseAdapter interface.
Reads a JSON file containing an array of messages and replays them
with configurable delay, emitting normalized OrderBook updates.

Fixture file format (array of messages):
[
    {
        "type": "snapshot",
        "venue": "kalshi",
        "timestamp": 1700000000.0,
        "bids": [[price_cents, size], ...],
        "asks": [[price_cents, size], ...],
        "delay_ms": 0
    },
    {
        "type": "delta",
        "venue": "kalshi",
        "timestamp": 1700000001.0,
        "side": "bid",
        "price": 55,
        "size": 120.0,
        "delay_ms": 500
    },
    ...
]

Or Polymarket-style with decimal prices:
[
    {
        "type": "snapshot",
        "venue": "polymarket",
        "timestamp": 1700000000.0,
        "bids": [{"price": "0.55", "size": "100"}, ...],
        "asks": [{"price": "0.58", "size": "200"}, ...],
        "delay_ms": 0
    },
    ...
]
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from server.adapters.base import BaseAdapter, BookUpdateCallback
from server.models import OrderBook, PriceLevel, Venue

logger = logging.getLogger(__name__)


class FixtureAdapter(BaseAdapter):
    """
    Replay adapter that reads from a JSON fixture file.
    Loops the fixture indefinitely until stopped.
    """

    def __init__(
        self,
        market_id: str,
        on_book_update: BookUpdateCallback,
        fixture_path: str,
        venue: Venue = Venue.KALSHI,
        replay_speed: float = 1.0,
        loop: bool = True,
    ):
        super().__init__(market_id, on_book_update)
        self._fixture_path = Path(fixture_path)
        self._venue = venue
        self._replay_speed = replay_speed
        self._loop = loop
        self._messages: list[dict] = []

        # Internal book state
        self._bids: dict[int, float] = {}
        self._asks: dict[int, float] = {}

    @property
    def venue(self) -> Venue:
        return self._venue

    def _load_fixture(self) -> list[dict]:
        """Load and validate the fixture file."""
        if not self._fixture_path.exists():
            raise FileNotFoundError(f"Fixture not found: {self._fixture_path}")

        with open(self._fixture_path, "r") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError("Fixture must be a JSON array of messages")

        logger.info(f"[fixture] Loaded {len(data)} messages from {self._fixture_path}")
        return data

    async def _connect(self) -> None:
        """Replay fixture messages with delays."""
        self._messages = self._load_fixture()
        self._bids.clear()
        self._asks.clear()

        while self._running:
            for msg in self._messages:
                if not self._running:
                    return

                delay_ms = msg.get("delay_ms", 500)
                delay_s = (delay_ms / 1000.0) / self._replay_speed
                if delay_s > 0:
                    await asyncio.sleep(delay_s)

                await self._handle_fixture_message(msg)

            if not self._loop:
                logger.info("[fixture] Replay complete (no loop)")
                return

            logger.debug("[fixture] Looping replay")

    async def _disconnect(self) -> None:
        self._messages.clear()

    async def _handle_fixture_message(self, msg: dict) -> None:
        """Process a single fixture message."""
        msg_type = msg.get("type", "snapshot")

        if msg_type == "snapshot":
            self._bids.clear()
            self._asks.clear()

            for bid in msg.get("bids", []):
                price, size = self._parse_level(bid)
                if size > 0:
                    self._bids[price] = size

            for ask in msg.get("asks", []):
                price, size = self._parse_level(ask)
                if size > 0:
                    self._asks[price] = size

            await self._publish_book(is_snapshot=True)

        elif msg_type == "delta":
            side = msg.get("side", "bid")
            price = int(msg.get("price", 0))
            size = float(msg.get("size", 0))

            book = self._bids if side == "bid" else self._asks

            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size

            await self._publish_book(is_snapshot=False)

    def _parse_level(self, level) -> tuple[int, float]:
        """
        Parse a price level from either format:
        - [price_cents, size]  (Kalshi-style)
        - {"price": "0.55", "size": "100"}  (Polymarket-style)
        """
        if isinstance(level, (list, tuple)):
            return int(level[0]), float(level[1])
        elif isinstance(level, dict):
            price_str = level.get("price", "0")
            size_str = level.get("size", "0")
            # Detect if price is already in cents or decimal
            price_f = float(price_str)
            if price_f < 1.0:
                price_cents = round(price_f * 100)
            else:
                price_cents = int(price_f)
            return price_cents, float(size_str)
        else:
            return 0, 0.0

    async def _publish_book(self, is_snapshot: bool) -> None:
        bids = sorted(
            [PriceLevel(price=p, size=s) for p, s in self._bids.items()],
            key=lambda lvl: lvl.price,
            reverse=True,
        )

        asks = sorted(
            [PriceLevel(price=p, size=s) for p, s in self._asks.items()],
            key=lambda lvl: lvl.price,
        )

        book = OrderBook(
            venue=self._venue,
            market_id=self.market_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
            is_snapshot=is_snapshot,
        )
        await self._emit(book)