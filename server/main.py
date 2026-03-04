"""
Application entrypoint.

Wires together:
  - Config loading
  - Adapter creation (live or fixture per venue)
  - Consolidator
  - WebSocket stream server
  - HTTP server for serving the frontend static files

Runs everything on a single asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from server.config import load_config, AppConfig
from server.adapters.base import BookUpdateCallback
from server.adapters.kalshi import KalshiAdapter
from server.adapters.polymarket import PolymarketAdapter
from server.adapters.fixture import FixtureAdapter
from server.engine.consolidator import Consolidator
from server.stream.ws_server import StreamServer
from server.models import Venue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def create_adapter(
    venue: Venue,
    config: AppConfig,
    callback: BookUpdateCallback,
):
    """Factory: create the right adapter based on config."""
    if venue == Venue.KALSHI:
        if config.kalshi.use_fixture:
            logger.info("[main] Using fixture adapter for Kalshi")
            return FixtureAdapter(
                market_id=config.kalshi.market_ticker,
                on_book_update=callback,
                fixture_path=config.kalshi.fixture_path,
                venue=Venue.KALSHI,
            )
        else:
            logger.info("[main] Using live adapter for Kalshi")
            return KalshiAdapter(
                market_id=config.kalshi.market_ticker,
                on_book_update=callback,
                api_key_id=config.kalshi.api_key_id,
                private_key_path=config.kalshi.private_key_path,
                use_demo=config.kalshi.use_demo,
            )

    elif venue == Venue.POLYMARKET:
        if config.polymarket.use_fixture:
            logger.info("[main] Using fixture adapter for Polymarket")
            return FixtureAdapter(
                market_id=config.polymarket.token_id or "polymarket",
                on_book_update=callback,
                fixture_path=config.polymarket.fixture_path,
                venue=Venue.POLYMARKET,
            )
        else:
            logger.info("[main] Using live adapter for Polymarket")
            return PolymarketAdapter(
                market_id=config.polymarket.condition_id or config.polymarket.token_id,
                on_book_update=callback,
                token_id=config.polymarket.token_id,
            )

    else:
        raise ValueError(f"Unknown venue: {venue}")


async def serve_static(config: AppConfig) -> None:
    """
    Serve frontend static files via a simple HTTP server.
    Runs on port 8080 so it doesn't conflict with the WS server.
    """
    from aiohttp import web

    static_dir = Path(__file__).parent.parent / config.server.static_dir

    app = web.Application()

    async def index_handler(request):
        index_path = static_dir / "templates" / "index.html"
        if index_path.exists():
            return web.FileResponse(index_path)
        return web.Response(text="Frontend not found", status=404)

    app.router.add_get("/", index_handler)

    if (static_dir / "static").exists():
        app.router.add_static("/static/", static_dir / "static")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("[main] HTTP server for frontend at http://localhost:8080")


async def main() -> None:
    config = load_config()

    logger.info(f"[main] Market: {config.market_name} / {config.outcome}")
    logger.info(f"[main] Stale threshold: {config.stale_threshold_s}s")

    # Create stream server
    stream = StreamServer(
        host=config.server.host,
        port=config.server.port,
    )

    # Create consolidator, wired to stream server
    consolidator = Consolidator(
        market_id=config.kalshi.market_ticker,
        stale_threshold_s=config.stale_threshold_s,
        staleness_check_interval_s=config.staleness_check_interval_s,
        on_consolidated_update=stream.on_consolidated_update,
    )

    # Register venues
    consolidator.register_venue(Venue.KALSHI)
    consolidator.register_venue(Venue.POLYMARKET)

    # Create adapters — both feed into consolidator.on_book_update
    kalshi_adapter = create_adapter(Venue.KALSHI, config, consolidator.on_book_update)
    poly_adapter = create_adapter(Venue.POLYMARKET, config, consolidator.on_book_update)

    # Start everything
    await stream.start()
    await consolidator.start()
    await kalshi_adapter.start()
    await poly_adapter.start()

    # Serve frontend static files
    try:
        await serve_static(config)
    except Exception:
        logger.warning("[main] Could not start HTTP server for frontend (aiohttp missing?)")

    logger.info("[main] System running. Press Ctrl+C to stop.")

    # Wait for shutdown signal
    stop_event = asyncio.Event()

    def on_signal():
        logger.info("[main] Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass

    # Graceful shutdown
    logger.info("[main] Shutting down...")
    await kalshi_adapter.stop()
    await poly_adapter.stop()
    await consolidator.stop()
    await stream.stop()
    logger.info("[main] Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())