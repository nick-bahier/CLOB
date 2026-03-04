/**
 * Frontend client for the consolidated order book.
 *
 * Connects to the backend WS /stream endpoint, receives
 * ConsolidatedBook payloads, and renders:
 *   - Aggregated ladder (bid size | price | ask size)
 *   - Venue-split ladder (per-venue + totals)
 *   - Crossed state banner
 *   - Venue status indicators with stale warnings
 */

(function () {
    "use strict";

    // ── Config ──
    const WS_URL = `ws://${window.location.hostname}:8765`;
    const RECONNECT_INTERVAL = 2000;
    const MAX_LEVELS = 50; // max price levels to display

    // ── DOM refs ──
    const marketNameEl = document.getElementById("market-name");
    const venueStatusesEl = document.getElementById("venue-statuses");
    const wsStatusEl = document.getElementById("ws-status");
    const crossedBanner = document.getElementById("crossed-banner");
    const crossedText = document.getElementById("crossed-text");
    const aggBody = document.getElementById("agg-body");
    const splitBody = document.getElementById("split-body");
    const lastUpdateEl = document.getElementById("last-update");

    let ws = null;
    let reconnectTimer = null;

    // ── WebSocket Connection ──

    function connect() {
        if (ws && ws.readyState <= 1) return;

        ws = new WebSocket(WS_URL);

        ws.onopen = function () {
            wsStatusEl.textContent = "Connected";
            wsStatusEl.className = "ws-status connected";
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
        };

        ws.onmessage = function (event) {
            try {
                const data = JSON.parse(event.data);
                render(data);
            } catch (e) {
                console.error("Failed to parse message:", e);
            }
        };

        ws.onclose = function () {
            wsStatusEl.textContent = "Disconnected";
            wsStatusEl.className = "ws-status disconnected";
            scheduleReconnect();
        };

        ws.onerror = function () {
            ws.close();
        };
    }

    function scheduleReconnect() {
        if (!reconnectTimer) {
            reconnectTimer = setTimeout(function () {
                reconnectTimer = null;
                connect();
            }, RECONNECT_INTERVAL);
        }
    }

    // ── Rendering ──

    function render(data) {
        renderHeader(data);
        renderCrossed(data.crossed);
        renderAggregatedLadder(data.aggregated_bids, data.aggregated_asks, data.crossed);
        renderVenueSplitLadder(data.aggregated_bids, data.aggregated_asks, data.crossed);
        renderFooter(data.timestamp);
    }

    function renderHeader(data) {
        marketNameEl.textContent = data.market_id || "—";
        renderVenueStatuses(data.venue_statuses || []);
    }

    function renderVenueStatuses(statuses) {
        let html = "";
        for (const s of statuses) {
            const name = s.venue.charAt(0).toUpperCase() + s.venue.slice(1);
            let dotClass = "venue-dot";
            if (s.connected && !s.stale) dotClass += " connected";
            else if (s.stale) dotClass += " stale";

            const timeStr = s.last_update > 0
                ? new Date(s.last_update * 1000).toLocaleTimeString()
                : "—";

            let staleTag = s.stale ? '<span class="stale-tag">STALE</span>' : "";

            html += `
                <div class="venue-status">
                    <span class="${dotClass}"></span>
                    <span>${name}${staleTag}</span>
                    <span class="venue-time">${timeStr}</span>
                </div>
            `;
        }
        venueStatusesEl.innerHTML = html;
    }

    function renderCrossed(crossed) {
        if (crossed && crossed.is_crossed) {
            const bidVenues = (crossed.best_bid_venues || []).join(", ");
            const askVenues = (crossed.best_ask_venues || []).join(", ");
            crossedText.textContent =
                `CROSSED — Best Bid: ${crossed.best_bid}¢ (${bidVenues}) × ` +
                `Best Ask: ${crossed.best_ask}¢ (${askVenues})`;
            crossedBanner.classList.remove("hidden");
        } else {
            crossedBanner.classList.add("hidden");
        }
    }

    function renderAggregatedLadder(bids, asks, crossed) {
        // Collect all prices and build a unified ladder
        const prices = collectAllPrices(bids, asks);
        const bidMap = indexByPrice(bids);
        const askMap = indexByPrice(asks);
        const maxSize = findMaxSize(bids, asks);

        let html = "";
        for (const price of prices) {
            const bid = bidMap[price];
            const ask = askMap[price];
            const isCrossed = isCrossedPrice(price, crossed);
            const rowClass = isCrossed ? "crossed-row" : "";

            const bidSize = bid ? bid.total_size : 0;
            const askSize = ask ? ask.total_size : 0;
            const bidPct = maxSize > 0 ? (bidSize / maxSize) * 100 : 0;
            const askPct = maxSize > 0 ? (askSize / maxSize) * 100 : 0;

            html += `<tr class="${rowClass}">`;
            html += bidSize > 0
                ? `<td class="bid-cell">${formatSize(bidSize)}<span class="size-bar" style="width:${bidPct}%"></span></td>`
                : `<td class="empty-cell">—</td>`;
            html += `<td class="price-cell">${price}¢</td>`;
            html += askSize > 0
                ? `<td class="ask-cell">${formatSize(askSize)}<span class="size-bar" style="width:${askPct}%"></span></td>`
                : `<td class="empty-cell">—</td>`;
            html += `</tr>`;
        }

        aggBody.innerHTML = html;
    }

    function renderVenueSplitLadder(bids, asks, crossed) {
        const prices = collectAllPrices(bids, asks);
        const bidMap = indexByPrice(bids);
        const askMap = indexByPrice(asks);

        let html = "";
        for (const price of prices) {
            const bid = bidMap[price];
            const ask = askMap[price];
            const isCr = isCrossedPrice(price, crossed);
            const rowClass = isCr ? "crossed-row" : "";

            // Extract per-venue sizes
            const bidKalshi = getVenueSize(bid, "kalshi");
            const bidPoly = getVenueSize(bid, "polymarket");
            const bidTotal = bid ? bid.total_size : 0;
            const askTotal = ask ? ask.total_size : 0;
            const askKalshi = getVenueSize(ask, "kalshi");
            const askPoly = getVenueSize(ask, "polymarket");

            // Check staleness per venue
            const bidKalshiStale = isVenueStale(bid, "kalshi");
            const bidPolyStale = isVenueStale(bid, "polymarket");
            const askKalshiStale = isVenueStale(ask, "kalshi");
            const askPolyStale = isVenueStale(ask, "polymarket");

            html += `<tr class="${rowClass}">`;
            html += formatVenueCell(bidKalshi, "bid", bidKalshiStale);
            html += formatVenueCell(bidPoly, "bid", bidPolyStale);
            html += bidTotal > 0
                ? `<td class="bid-cell">${formatSize(bidTotal)}</td>`
                : `<td class="empty-cell">—</td>`;
            html += `<td class="price-cell">${price}¢</td>`;
            html += askTotal > 0
                ? `<td class="ask-cell">${formatSize(askTotal)}</td>`
                : `<td class="empty-cell">—</td>`;
            html += formatVenueCell(askKalshi, "ask", askKalshiStale);
            html += formatVenueCell(askPoly, "ask", askPolyStale);
            html += `</tr>`;
        }

        splitBody.innerHTML = html;
    }

    function renderFooter(timestamp) {
        if (timestamp) {
            const d = new Date(timestamp * 1000);
            lastUpdateEl.textContent = "Last update: " + d.toLocaleTimeString();
        }
    }

    // ── Helpers ──

    function collectAllPrices(bids, asks) {
        const priceSet = new Set();
        for (const b of (bids || [])) priceSet.add(b.price);
        for (const a of (asks || [])) priceSet.add(a.price);

        // Sort descending so highest prices are at top
        const prices = Array.from(priceSet).sort(function (a, b) { return b - a; });

        // Limit to MAX_LEVELS
        if (prices.length > MAX_LEVELS) {
            return prices.slice(0, MAX_LEVELS);
        }
        return prices;
    }

    function indexByPrice(levels) {
        const map = {};
        for (const lvl of (levels || [])) {
            map[lvl.price] = lvl;
        }
        return map;
    }

    function findMaxSize(bids, asks) {
        let max = 0;
        for (const b of (bids || [])) { if (b.total_size > max) max = b.total_size; }
        for (const a of (asks || [])) { if (a.total_size > max) max = a.total_size; }
        return max;
    }

    function isCrossedPrice(price, crossed) {
        if (!crossed || !crossed.is_crossed) return false;
        return price >= crossed.best_ask && price <= crossed.best_bid;
    }

    function getVenueSize(level, venueName) {
        if (!level || !level.venue_sizes) return 0;
        for (const vs of level.venue_sizes) {
            if (vs.venue === venueName) return vs.size;
        }
        return 0;
    }

    function isVenueStale(level, venueName) {
        if (!level || !level.venue_sizes) return false;
        for (const vs of level.venue_sizes) {
            if (vs.venue === venueName) return vs.stale;
        }
        return false;
    }

    function formatVenueCell(size, side, stale) {
        const cls = side === "bid" ? "bid-cell" : "ask-cell";
        if (size > 0) {
            const staleClass = stale ? " stale" : "";
            const staleTag = stale ? '<span class="stale-tag">S</span>' : "";
            return `<td class="${cls}${staleClass}">${formatSize(size)}${staleTag}</td>`;
        }
        return `<td class="empty-cell">—</td>`;
    }

    function formatSize(size) {
        if (size >= 1000) {
            return (size / 1000).toFixed(1) + "k";
        }
        return size % 1 === 0 ? size.toString() : size.toFixed(1);
    }

    // ── Init ──
    connect();

})();