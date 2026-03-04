"""
Consolidation engine.

Takes per-venue books and produces:
  1. Aggregated ladder — sizes summed across venues at each price
  2. Venue-split ladder — size by venue at each price
  3. Crossed state — whether consolidated best bid >= best ask

Optimized for incremental updates:
  - On delta updates, only recomputes price levels that changed
  - Maintains a cached consolidated state, patching it per tick
  - Full rebuild only on snapshots or when cache is invalid

Also runs a periodic staleness check timer so stale flags
update even when no data is flowing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable, Optional

from server.models import (
    AggregatedLevel,
    ConsolidatedBook,
    CrossedState,
    OrderBook,
    Venue,
    VenueLevelDetail,
)
from server.engine.book import VenueBook
from server.engine.staleness import StalenessTracker

logger = logging.getLogger(__name__)

ConsolidatedCallback = Callable[[ConsolidatedBook], Awaitable[None]]


class Consolidator:
    """
    Central consolidation engine.

    Receives book updates from adapters, maintains per-venue books,
    and recomputes the consolidated view on each update.

    Optimizations:
      - Caches aggregated bids/asks as dict[int, AggregatedLevel]
      - On deltas, only recomputes the changed price levels
      - Runs a staleness timer to emit updates when data goes stale
    """

    def __init__(
        self,
        market_id: str,
        stale_threshold_s: float = 5.0,
        staleness_check_interval_s: float = 1.0,
        on_consolidated_update: Optional[ConsolidatedCallback] = None,
    ):
        self.market_id = market_id
        self._staleness = StalenessTracker(stale_threshold_s)
        self._books: dict[Venue, VenueBook] = {}
        self._on_update = on_consolidated_update
        self._lock = asyncio.Lock()
        self._staleness_check_interval = staleness_check_interval_s
        self._staleness_task: Optional[asyncio.Task] = None

        # Cached aggregated state: price -> AggregatedLevel
        self._cached_bids: dict[int, AggregatedLevel] = {}
        self._cached_asks: dict[int, AggregatedLevel] = {}
        self._cache_valid = False

    def register_venue(self, venue: Venue) -> None:
        if venue not in self._books:
            self._books[venue] = VenueBook(venue, self.market_id)

    async def start(self) -> None:
        """Start the periodic staleness check timer."""
        self._staleness_task = asyncio.create_task(self._staleness_loop())

    async def stop(self) -> None:
        """Stop the staleness check timer."""
        if self._staleness_task:
            self._staleness_task.cancel()
            try:
                await self._staleness_task
            except asyncio.CancelledError:
                pass

    async def _staleness_loop(self) -> None:
        """Periodically recheck staleness and emit if anything changed."""
        last_stale_state: dict[Venue, bool] = {}
        while True:
            try:
                await asyncio.sleep(self._staleness_check_interval)

                current_stale = {
                    v: self._staleness.is_stale(v) for v in self._books
                }

                # Only emit if staleness changed for any venue
                if current_stale != last_stale_state:
                    last_stale_state = current_stale
                    await self.force_refresh()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in staleness check loop")

    async def on_book_update(self, book: OrderBook) -> None:
        """
        Callback for adapters to push updates into the consolidator.
        Pass this as on_book_update to adapter constructors.
        """
        async with self._lock:
            venue = book.venue

            if venue not in self._books:
                self._books[venue] = VenueBook(venue, self.market_id)

            vbook = self._books[venue]
            vbook.apply(book)
            self._staleness.record_update(venue)

            # Incremental or full rebuild
            if book.is_snapshot or not self._cache_valid:
                self._full_rebuild()
            else:
                self._incremental_update(
                    vbook.changed_bid_prices,
                    vbook.changed_ask_prices,
                )

            consolidated = self._build_output()

        if self._on_update:
            try:
                await self._on_update(consolidated)
            except Exception:
                logger.exception("Error in consolidated update callback")

    def _full_rebuild(self) -> None:
        """Rebuild the entire cached aggregated state from scratch."""
        self._cached_bids.clear()
        self._cached_asks.clear()

        for venue, vbook in self._books.items():
            stale = self._staleness.is_stale(venue)

            for price, size in vbook.bids.items():
                self._add_to_cache(self._cached_bids, price, venue, size, stale)

            for price, size in vbook.asks.items():
                self._add_to_cache(self._cached_asks, price, venue, size, stale)

        self._cache_valid = True

    def _incremental_update(
        self,
        changed_bids: set[int],
        changed_asks: set[int],
    ) -> None:
        """Only recompute the price levels that changed."""
        for price in changed_bids:
            self._recompute_price(self._cached_bids, price, "bid")

        for price in changed_asks:
            self._recompute_price(self._cached_asks, price, "ask")

    def _recompute_price(
        self,
        cache: dict[int, AggregatedLevel],
        price: int,
        side: str,
    ) -> None:
        """Recompute a single price level across all venues."""
        venue_details: list[VenueLevelDetail] = []
        total = 0.0

        for venue, vbook in self._books.items():
            levels = vbook.bids if side == "bid" else vbook.asks
            size = levels.get(price, 0.0)
            if size > 0:
                stale = self._staleness.is_stale(venue)
                venue_details.append(
                    VenueLevelDetail(venue=venue, size=size, stale=stale)
                )
                total += size

        if venue_details:
            cache[price] = AggregatedLevel(
                price=price,
                total_size=total,
                venue_sizes=venue_details,
            )
        else:
            cache.pop(price, None)

    def _add_to_cache(
        self,
        cache: dict[int, AggregatedLevel],
        price: int,
        venue: Venue,
        size: float,
        stale: bool,
    ) -> None:
        """Add a single venue's contribution to a cached price level."""
        if price not in cache:
            cache[price] = AggregatedLevel(
                price=price, total_size=0.0, venue_sizes=[]
            )
        level = cache[price]
        level.venue_sizes.append(
            VenueLevelDetail(venue=venue, size=size, stale=stale)
        )
        level.total_size += size

    def _build_output(self) -> ConsolidatedBook:
        """Assemble the final ConsolidatedBook from cached state."""
        agg_bids = sorted(
            self._cached_bids.values(),
            key=lambda lvl: lvl.price,
            reverse=True,
        )
        agg_asks = sorted(
            self._cached_asks.values(),
            key=lambda lvl: lvl.price,
        )
        crossed = self._detect_crossed(agg_bids, agg_asks)
        statuses = self._staleness.get_all_statuses()

        return ConsolidatedBook(
            market_id=self.market_id,
            aggregated_bids=agg_bids,
            aggregated_asks=agg_asks,
            crossed=crossed,
            venue_statuses=statuses,
            timestamp=time.time(),
        )

    def _detect_crossed(
        self,
        agg_bids: list[AggregatedLevel],
        agg_asks: list[AggregatedLevel],
    ) -> CrossedState:
        if not agg_bids or not agg_asks:
            return CrossedState(is_crossed=False)

        best_bid = agg_bids[0]
        best_ask = agg_asks[0]
        is_crossed = best_bid.price >= best_ask.price

        return CrossedState(
            is_crossed=is_crossed,
            best_bid=best_bid.price,
            best_bid_size=best_bid.total_size,
            best_bid_venues=[vd.venue for vd in best_bid.venue_sizes],
            best_ask=best_ask.price,
            best_ask_size=best_ask.total_size,
            best_ask_venues=[vd.venue for vd in best_ask.venue_sizes],
        )

    async def force_refresh(self) -> Optional[ConsolidatedBook]:
        """Force a full recompute and emit. Used by staleness timer."""
        async with self._lock:
            self._full_rebuild()
            consolidated = self._build_output()

        if self._on_update:
            try:
                await self._on_update(consolidated)
            except Exception:
                logger.exception("Error in consolidated update callback")

        return consolidated