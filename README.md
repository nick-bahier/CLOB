# Consolidated Order Book (CLOB)

Pulls order book data from Kalshi and Polymarket for a prediction market, merges them together, and shows it in a browser with live updates.

> **Note:** The default market configured is "US Recession by End of 2026" — a Yes/No binary market tracking the odds of a US recession. Polymarket is streaming live order book data. Kalshi is using recorded fixture data because the live setup is slow. The live Polymarket feed updates infrequently since prediction markets are much quieter than stock exchanges — updates only happen when someone places, cancels, or fills an order.
---

## How to Run

You need Python 3.11 or higher.
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m server.main
```

Then open http://localhost:8080 in your browser.

It runs in fixture mode by default so you don't need any API keys. It just replays some fake order book data on loop.

If you get SSL errors on macOS, run this before starting the server:
```bash
pip install certifi
export SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")
```

---

## Changing the Market

Edit `config/default.json`. The main fields that matter:

- `market_name` — just a display label
- `kalshi.market_ticker` — the ticker from Kalshi
- `polymarket.token_id` — the token ID from Polymarket
- `use_fixture` — set to `false` on either venue to go live

You can also override anything with env vars:
```bash
CLOB_KALSHI_USE_FIXTURE=false CLOB_KALSHI_MARKET_TICKER=PRES-2028-DEM python -m server.main
```

To find a Polymarket token ID for a market:
```bash
curl "https://gamma-api.polymarket.com/markets?slug=YOUR-MARKET-SLUG" | python -m json.tool
```

Look for `clobTokenIds` — the first one is the Yes outcome.

Kalshi requires API keys for live WebSocket access. Generate them in your Kalshi account settings and set `api_key_id` and `private_key_path` in the config. Polymarket market channel is public, no auth needed.

---

## What's Done

- Kalshi and Polymarket WebSocket adapters that normalize into the same format
- Fixture adapter that replays recorded JSON so you can run without API keys
- All three adapters share the same interface so they're swappable in config
- Consolidation engine that merges books, sums sizes across venues, and detects crossed state
- Incremental updates — only recomputes price levels that actually changed
- Staleness tracking — flags a venue as stale if no update in 5 seconds
- Reconnection with exponential backoff if a venue disconnects
- WebSocket server that streams the consolidated book to the browser
- Frontend with aggregated ladder, venue-split ladder, crossed banner, and stale warnings
- Config file with env var overrides

## What's Partial

- **Snapshot gating after reconnect** — both venues send a snapshot when you subscribe so it works fine, but I didn't add an explicit check in the consolidator to wait for it
- **Live testing** — Polymarket adapter tested live against production WebSocket. Kalshi adapter built from docs but not tested live since it requires API keys and active markets

## What's Missing

- No tests
- No render throttling on the frontend (it rebuilds the whole table every tick)
- No delta streaming to the frontend (sends the full book every time)
- No TLS on the WebSocket
- Only supports one market at a time

---

## Tradeoffs

- **Adapters emit full books, not deltas.** Simpler contract between adapters and the consolidator. Prediction market books are tiny (max 99 price levels) so it doesn't matter.
- **Used dataclasses instead of Pydantic.** Fewer dependencies. Validation isn't really needed since the data only flows through internal code paths.
- **Frontend uses innerHTML to re-render.** Easy to write but could flicker if updates are really fast. Would need DOM diffing to fix.
- **Everything on one async event loop.** Works for prediction markets where you get maybe 10-50 updates per second. Wouldn't scale to equity market feeds.
- **Kalshi only gives bids.** So I convert NO bids into YES asks (NO bid at Y¢ = YES ask at 100-Y¢). That's just how binary markets work.

---

## Next Steps

1. Test Kalshi adapter live against real API with active markets
2. Write unit tests
3. Throttle frontend rendering to ~10fps
4. Send deltas to the frontend instead of full books
5. Add snapshot gating after reconnects
6. Support multiple markets
7. Add TLS
8. Better logging and metrics