"""
Per-venue order book manager.

Maintains a normalized book for a single venue using dictionaries
keyed by price (int cents). Supports both full snapshot replacement
and true incremental level-by-level delta application.
"""

from __future__ import annotations

import time
from typing import Optional

from server.models import OrderBook, PriceLevel, Venue


class VenueBook:
    """
    Efficient mutable order book for one venue.

    Supports two update modes:
      - snapshot: clear and rebuild (is_snapshot=True)
      - delta: apply individual level changes (is_snapshot=False)
    """

    def __init__(self, venue: Venue, market_id: str):
        self.venue = venue
        self.market_id = market_id
        self.last_update: float = 0.0
        self.has_snapshot: bool = False

        # price_cents -> size
        self._bids: dict[int, float] = {}
        self._asks: dict[int, float] = {}

        # Track which prices changed on last apply, so the
        # consolidator can do minimal recompute
        self.changed_bid_prices: set[int] = set()
        self.changed_ask_prices: set[int] = set()

    def apply(self, book: OrderBook) -> None:
        """
        Apply an incoming OrderBook update from an adapter.
        Snapshot: replace everything.
        Delta: only update the levels present in the update.
        """
        self.changed_bid_prices.clear()
        self.changed_ask_prices.clear()

        if book.is_snapshot:
            self._apply_snapshot(book)
        else:
            self._apply_delta(book)

        self.last_update = book.timestamp or time.time()

    def _apply_snapshot(self, book: OrderBook) -> None:
        """Full replacement — mark all old and new prices as changed."""
        # All existing prices are changing (being removed or replaced)
        self.changed_bid_prices.update(self._bids.keys())
        self.changed_ask_prices.update(self._asks.keys())

        self._bids.clear()
        self._asks.clear()

        for level in book.bids:
            if level.size > 0:
                self._bids[level.price] = level.size
                self.changed_bid_prices.add(level.price)

        for level in book.asks:
            if level.size > 0:
                self._asks[level.price] = level.size
                self.changed_ask_prices.add(level.price)

        self.has_snapshot = True

    def _apply_delta(self, book: OrderBook) -> None:
        """
        Incremental update — only touch levels present in the delta.
        A level with size 0 means remove it.
        A level with size > 0 means set it to that size.
        """
        for level in book.bids:
            self.changed_bid_prices.add(level.price)
            if level.size <= 0:
                self._bids.pop(level.price, None)
            else:
                self._bids[level.price] = level.size

        for level in book.asks:
            self.changed_ask_prices.add(level.price)
            if level.size <= 0:
                self._asks.pop(level.price, None)
            else:
                self._asks[level.price] = level.size

    def clear(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.has_snapshot = False
        self.changed_bid_prices.clear()
        self.changed_ask_prices.clear()

    @property
    def bids(self) -> dict[int, float]:
        return self._bids

    @property
    def asks(self) -> dict[int, float]:
        return self._asks

    @property
    def best_bid(self) -> Optional[int]:
        return max(self._bids.keys()) if self._bids else None

    @property
    def best_ask(self) -> Optional[int]:
        return min(self._asks.keys()) if self._asks else None

    @property
    def is_empty(self) -> bool:
        return not self._bids and not self._asks

    def sorted_bids(self) -> list[tuple[int, float]]:
        return sorted(self._bids.items(), key=lambda x: x[0], reverse=True)

    def sorted_asks(self) -> list[tuple[int, float]]:
        return sorted(self._asks.items(), key=lambda x: x[0])