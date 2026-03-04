"""
Staleness tracker for venue connections.

Monitors the last update time per venue and flags any venue
as stale if no update has been received within the configured
threshold (default 5 seconds).
"""

from __future__ import annotations

import time
from typing import Optional

from server.models import Venue, VenueStatus


class StalenessTracker:
    """
    Tracks per-venue update timestamps and connection state.

    Call record_update() every time a venue sends data.
    Call get_status() to check current state of all venues.
    """

    def __init__(self, stale_threshold_s: float = 5.0):
        self._threshold = stale_threshold_s
        self._last_update: dict[Venue, float] = {}
        self._connected: dict[Venue, bool] = {}
        self._errors: dict[Venue, Optional[str]] = {}

    def record_update(self, venue: Venue) -> None:
        """Record that a venue just sent a valid update."""
        self._last_update[venue] = time.time()
        self._connected[venue] = True
        self._errors[venue] = None

    def record_disconnect(self, venue: Venue, error: Optional[str] = None) -> None:
        """Record that a venue has disconnected."""
        self._connected[venue] = False
        self._errors[venue] = error

    def record_connect(self, venue: Venue) -> None:
        """Record that a venue has connected."""
        self._connected[venue] = True
        self._errors[venue] = None

    def is_stale(self, venue: Venue) -> bool:
        """Check if a venue is stale (no update within threshold)."""
        last = self._last_update.get(venue)
        if last is None:
            return True
        return (time.time() - last) > self._threshold

    def get_status(self, venue: Venue) -> VenueStatus:
        """Get full status for a single venue."""
        return VenueStatus(
            venue=venue,
            connected=self._connected.get(venue, False),
            stale=self.is_stale(venue),
            last_update=self._last_update.get(venue, 0.0),
            error=self._errors.get(venue),
        )

    def get_all_statuses(self) -> list[VenueStatus]:
        """Get status for all known venues."""
        venues = set(self._last_update.keys()) | set(self._connected.keys())
        return [self.get_status(v) for v in sorted(venues, key=lambda v: v.value)]