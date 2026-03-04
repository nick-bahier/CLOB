"""
Kalshi WebSocket adapter.

Protocol notes (from Kalshi docs):
  - WS URL: wss://api.elections.kalshi.com/trade-api/ws/v2
    (demo: wss://demo-api.kalshi.co/trade-api/ws/v2)
  - Auth: RSA-PSS signed headers during WS handshake
  - Subscribe: {"id": N, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": "..."}}
  - Snapshot: type=orderbook_snapshot, msg.yes=[[price,size],...], msg.no=[[price,size],...]
  - Delta: type=orderbook_delta, msg.side=yes|no, msg.price=int, msg.delta=int
  - Kalshi only returns bids. A YES bid at X¢ = canonical bid at X¢.
    A NO bid at Y¢ = canonical ask at (100-Y)¢.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Optional

from server.adapters.base import BaseAdapter, BookUpdateCallback
from server.models import OrderBook, PriceLevel, Venue

logger = logging.getLogger(__name__)


class KalshiAdapter(BaseAdapter):

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"

    def __init__(
        self,
        market_id: str,
        on_book_update: BookUpdateCallback,
        api_key_id: str = "",
        private_key_path: str = "",
        use_demo: bool = False,
    ):
        super().__init__(market_id, on_book_update)
        self._api_key_id = api_key_id
        self._private_key_path = private_key_path
        self._ws_url = self.DEMO_WS_URL if use_demo else self.WS_URL
        self._ws = None
        self._cmd_id = 0
        self._has_snapshot = False

        # Internal state: price (int cents) -> size (float contracts)
        self._yes_bids: dict[int, float] = {}
        self._no_bids: dict[int, float] = {}

    @property
    def venue(self) -> Venue:
        return Venue.KALSHI

    def _next_cmd_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    def _build_auth_headers(self) -> dict[str, str]:
        """Build RSA-PSS authentication headers for the WS handshake."""
        if not self._api_key_id or not self._private_key_path:
            logger.warning(
                "[kalshi] No API credentials configured. "
                "Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH."
            )
            return {}

        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            with open(self._private_key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)

            timestamp_ms = str(int(time.time() * 1000))
            msg_string = timestamp_ms + "GET" + "/trade-api/ws/v2"
            signature = private_key.sign(
                msg_string.encode("utf-8"),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

            return {
                "KALSHI-ACCESS-KEY": self._api_key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
                "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            }
        except Exception:
            logger.exception("[kalshi] Failed to build auth headers")
            return {}

    async def _connect(self) -> None:
        import websockets

        headers = self._build_auth_headers()
        self._has_snapshot = False
        self._yes_bids.clear()
        self._no_bids.clear()

        logger.info(f"[kalshi] Connecting to {self._ws_url}")
        async with websockets.connect(
            self._ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[kalshi] Connected, subscribing to orderbook_delta")

            subscribe_msg = {
                "id": self._next_cmd_id(),
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_ticker": self.market_id,
                },
            }
            await ws.send(json.dumps(subscribe_msg))

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    await self._handle_message(msg)
                except json.JSONDecodeError:
                    logger.warning(f"[kalshi] Non-JSON message: {raw_msg[:200]}")
                except Exception:
                    logger.exception("[kalshi] Error handling message")

    async def _disconnect(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _handle_message(self, msg: dict) -> None:
        msg_type = msg.get("type")

        if msg_type == "orderbook_snapshot":
            await self._handle_snapshot(msg.get("msg", {}))
        elif msg_type == "orderbook_delta":
            await self._handle_delta(msg.get("msg", {}))
        elif msg_type == "error":
            error = msg.get("msg", {})
            logger.error(f"[kalshi] Server error: {error.get('code')} - {error.get('msg')}")

    async def _handle_snapshot(self, data: dict) -> None:
        """Process orderbook_snapshot: yes/no arrays of [price, size]."""
        self._yes_bids.clear()
        self._no_bids.clear()

        for price, size in data.get("yes", []):
            if size > 0:
                self._yes_bids[int(price)] = float(size)

        for price, size in data.get("no", []):
            if size > 0:
                self._no_bids[int(price)] = float(size)

        self._has_snapshot = True
        logger.debug(
            f"[kalshi] Snapshot: {len(self._yes_bids)} yes, {len(self._no_bids)} no levels"
        )
        await self._publish_book(is_snapshot=True)

    async def _handle_delta(self, data: dict) -> None:
        """Process orderbook_delta: side, price, delta."""
        if not self._has_snapshot:
            return

        side = data.get("side")
        price = int(data.get("price", 0))
        delta = float(data.get("delta", 0))

        if side == "yes":
            book = self._yes_bids
        elif side == "no":
            book = self._no_bids
        else:
            logger.warning(f"[kalshi] Unknown side: {side}")
            return

        new_size = book.get(price, 0.0) + delta
        if new_size <= 0:
            book.pop(price, None)
        else:
            book[price] = new_size

        await self._publish_book(is_snapshot=False)

    async def _publish_book(self, is_snapshot: bool) -> None:
        """
        Convert internal state to canonical OrderBook.
        YES bids at X  → canonical bids at X¢ (descending)
        NO bids at Y   → canonical asks at (100-Y)¢ (ascending)
        """
        bids = sorted(
            [PriceLevel(price=p, size=s) for p, s in self._yes_bids.items()],
            key=lambda lvl: lvl.price,
            reverse=True,
        )

        asks = sorted(
            [PriceLevel(price=100 - p, size=s) for p, s in self._no_bids.items()],
            key=lambda lvl: lvl.price,
        )

        book = OrderBook(
            venue=Venue.KALSHI,
            market_id=self.market_id,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
            is_snapshot=is_snapshot,
        )
        await self._emit(book)