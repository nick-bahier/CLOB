"""
Canonical data models for the consolidated order book system.
All prices are in integer cents (0-99 for prediction markets).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Side(str, Enum):
    BID = "bid"
    ASK = "ask"


@dataclass
class PriceLevel:
    """A single price level: price in cents, size in contracts."""
    price: int       # 1-99 cents
    size: float      # number of contracts/shares


@dataclass
class OrderBook:
    """
    Normalized order book from a single venue.
    - bids: descending by price (best bid first)
    - asks: ascending by price (best ask first)
    """
    venue: Venue
    market_id: str
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    is_snapshot: bool = False

    @property
    def best_bid(self) -> Optional[PriceLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[PriceLevel]:
        return self.asks[0] if self.asks else None


@dataclass
class VenueLevelDetail:
    """Size contributed by a single venue at a price level."""
    venue: Venue
    size: float
    stale: bool = False


@dataclass
class AggregatedLevel:
    """A price level aggregated across venues."""
    price: int
    total_size: float
    venue_sizes: list[VenueLevelDetail] = field(default_factory=list)


@dataclass
class CrossedState:
    """Indicates whether the consolidated book is crossed."""
    is_crossed: bool = False
    best_bid: Optional[int] = None
    best_bid_size: float = 0.0
    best_bid_venues: list[Venue] = field(default_factory=list)
    best_ask: Optional[int] = None
    best_ask_size: float = 0.0
    best_ask_venues: list[Venue] = field(default_factory=list)


@dataclass
class VenueStatus:
    """Status of a single venue connection."""
    venue: Venue
    connected: bool = False
    stale: bool = False
    last_update: float = 0.0
    error: Optional[str] = None


@dataclass
class ConsolidatedBook:
    """
    Full consolidated output streamed to the frontend.
    Contains everything the UI needs in one payload.
    """
    market_id: str
    aggregated_bids: list[AggregatedLevel] = field(default_factory=list)
    aggregated_asks: list[AggregatedLevel] = field(default_factory=list)
    crossed: CrossedState = field(default_factory=CrossedState)
    venue_statuses: list[VenueStatus] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)