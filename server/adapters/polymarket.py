"""
Polymarket WebSocket adapter.

Protocol notes (from Polymarket docs):
  - WS URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
  - No auth required for market channel (public data)
  - Subscribe on connect: {"type": "market", "assets_ids": ["TOKEN_ID"]}
  - book message: full snapshot with bids[{price, size}] and asks[{price, size}]
    Emitted on first subscribe and after each trade.
  - price_change message: incremental update with asset_id, price, size (new total), side (BUY/SELL)
    Emitted on new order placed or cancelled.
  - Prices are decimal strings like "0.48", sizes are decimal strings like "30"
  - We convert to integer cents for canonical model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from server.adapters.base import BaseAdapter, BookUpdateCallback
from server.models import OrderBook, PriceLevel, Venue

logger = logging.getLogger(__name__)


def _price_str_to_cents(price_str: str) -> int:
    """Convert a decimal price string like '0.48' or '.48' to integer cents (48)."""
    return round(float(price_str) * 100)


class PolymarketAdapter(BaseAdapter):

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        market_id: str,
        on_book_update: BookUpdateCallback,
        token_id: str = "",
    ):
        super().__init__(market_id, on_book_update)
        self._token_id = token_id
        self._ws = None
        self._has_snapshot = False

        self._bids: dict[int, float] = {}
        self._asks: dict[int, float] = {}

    @property
    def venue(self) -> Venue:
        return Venue.POLYMARKET

    async def _connect(self) -> None:
        import websockets

        self._has_snapshot = False
        self._bids.clear()
        self._asks.clear()

        logger.info(f"[polymarket] Connecting to {self.WS_URL}")
        async with websockets.connect(
            self.WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[polymarket] Connected, subscribing to market channel")

            subscribe_msg = {
                "type": "market",
                "assets_ids": [self._token_id],
            }
            await ws.send(json.dumps(subscribe_msg))

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    await self._handle_message(msg)
                except json.JSONDecodeError:
                    logger.warning(f"[polymarket] Non-JSON message: {raw_msg[:200]}")
                except Exception:
                    logger.exception("[polymarket] Error handling message")

    async def _disconnect(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _handle_message(self, msg) -> None:
        if isinstance(msg, list):
            for item in msg:
                await self._handle_message(item)
            return

        if not isinstance(msg, dict):
            return

        event_type = msg.get("event_type")

        if event_type == "book":
            await self._handle_book(msg)
        elif event_type == "price_change":
            await self._handle_price_change(msg)

    async def _handle_book(self, data: dict) -> None:
        self._bids.clear()
        self._asks.clear()

        for level in data.get("bids", []):
            price_cents = _price_str_to_cents(level["price"])
            size = float(level["size"])
            if size > 0:
                self._bids[price_cents] = size

        for level in data.get("asks", []):
            price_cents = _price_str_to_cents(level["price"])
            size = float(level["size"])
            if size > 0:
                self._asks[price_cents] = size

        self._has_snapshot = True
        logger.info(
            f"[polymarket] Book snapshot: {len(self._bids)} bids, {len(self._asks)} asks"
        )
        await self._publish_book(is_snapshot=True)

    async def _handle_price_change(self, data: dict) -> None:
        changes = data.get("price_changes", [])
        if not changes:
            changes = [data]

        updated = False
        for change in changes:
            price_cents = _price_str_to_cents(change["price"])
            new_size = float(change["size"])
            side = change.get("side", "").upper()

            if side == "BUY":
                book = self._bids
            elif side == "SELL":
                book = self._asks
            else:
                continue

            if new_size <= 0:
                book.pop(price_cents, None)
            else:
                book[price_cents] = new_size
            updated = True

        if updated and self._has_snapshot:
            await self._publish_book(is_snapshot=False)

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
            venue=Venue.POLYMARKET,
            market_id=self.market_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
            is_snapshot=is_snapshot,
        )
        await self._emit(book)
