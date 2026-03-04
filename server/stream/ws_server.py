"""
WebSocket streaming server.

Exposes a WS /stream endpoint that pushes consolidated book
updates to all connected frontend clients.

Responsibilities:
  - Accept client WS connections
  - Fan out ConsolidatedBook updates to all connected clients
  - Serialize payloads to JSON
  - Handle client disconnects gracefully
  - Send the latest snapshot immediately on new client connect
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Optional

from server.models import ConsolidatedBook, Venue

logger = logging.getLogger(__name__)


class StreamServer:
    """
    WebSocket fan-out server.

    The consolidator calls on_consolidated_update() with each new
    ConsolidatedBook. This class serializes it and pushes to all
    connected clients.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self._host = host
        self._port = port
        self._clients: set = set()
        self._latest: Optional[str] = None  # cached JSON of last update
        self._server = None

    async def start(self) -> None:
        """Start the WebSocket server."""
        import websockets

        self._server = await websockets.serve(
            self._handle_client,
            self._host,
            self._port,
        )
        logger.info(f"[stream] WebSocket server listening on ws://{self._host}:{self._port}/stream")

    async def stop(self) -> None:
        """Shut down the server and disconnect all clients."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Close all client connections
        if self._clients:
            await asyncio.gather(
                *[self._close_client(ws) for ws in self._clients],
                return_exceptions=True,
            )
            self._clients.clear()

        logger.info("[stream] Server stopped")

    async def _handle_client(self, websocket, path=None) -> None:
        """Handle a new client connection."""
        self._clients.add(websocket)
        remote = websocket.remote_address
        logger.info(f"[stream] Client connected: {remote} ({len(self._clients)} total)")

        try:
            # Send the latest snapshot immediately so the client
            # doesn't have to wait for the next update
            if self._latest:
                await websocket.send(self._latest)

            # Keep the connection alive — we only send, never receive
            # meaningful data, but we need to consume incoming frames
            # (pings, close) to keep the connection healthy
            async for msg in websocket:
                # Clients don't send us anything meaningful
                # but we consume messages to detect disconnects
                pass

        except Exception:
            pass
        finally:
            self._clients.discard(websocket)
            logger.info(f"[stream] Client disconnected: {remote} ({len(self._clients)} total)")

    async def on_consolidated_update(self, book: ConsolidatedBook) -> None:
        """
        Called by the consolidator on every update.
        Serializes and fans out to all connected clients.
        """
        try:
            payload = self._serialize(book)
            self._latest = payload
        except Exception:
            logger.exception("[stream] Failed to serialize consolidated book")
            return

        if not self._clients:
            return

        # Fan out to all clients concurrently
        # Remove dead connections on failure
        dead: list = []
        tasks = []

        for ws in self._clients:
            tasks.append(self._send_to_client(ws, payload, dead))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Clean up dead connections
        for ws in dead:
            self._clients.discard(ws)

    async def _send_to_client(self, ws, payload: str, dead: list) -> None:
        """Send payload to a single client, marking it dead on failure."""
        try:
            await ws.send(payload)
        except Exception:
            dead.append(ws)

    async def _close_client(self, ws) -> None:
        """Gracefully close a client connection."""
        try:
            await ws.close()
        except Exception:
            pass

    def _serialize(self, book: ConsolidatedBook) -> str:
        """
        Serialize a ConsolidatedBook to JSON.

        Converts dataclasses to dicts and handles Venue enums.
        Output format:
        {
            "market_id": "...",
            "timestamp": 1700000000.0,
            "aggregated_bids": [
                {"price": 55, "total_size": 250.0, "venue_sizes": [
                    {"venue": "kalshi", "size": 150.0, "stale": false},
                    {"venue": "polymarket", "size": 100.0, "stale": false}
                ]},
                ...
            ],
            "aggregated_asks": [...],
            "crossed": {
                "is_crossed": false,
                "best_bid": 55,
                "best_bid_size": 250.0,
                "best_bid_venues": ["kalshi", "polymarket"],
                "best_ask": 58,
                "best_ask_size": 200.0,
                "best_ask_venues": ["kalshi"]
            },
            "venue_statuses": [
                {"venue": "kalshi", "connected": true, "stale": false,
                 "last_update": 1700000000.0, "error": null},
                ...
            ]
        }
        """
        raw = asdict(book)
        return json.dumps(raw, default=self._json_default)

    @staticmethod
    def _json_default(obj):
        """Handle non-serializable types."""
        if isinstance(obj, Venue):
            return obj.value
        raise TypeError(f"Not JSON serializable: {type(obj)}")