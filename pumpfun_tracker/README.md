# 🚀 Rocket Radar: a pump.fun momentum tracker

Watches every new pump.fun launch in real time, scores each token on how hard
**and how organically** it is pumping, and alerts you (dashboard, browser
notification, Discord, Telegram) when one starts skyrocketing.

> Signals only, not financial advice. Most pump.fun tokens go to zero. A high
> score means "lots of broad buying right now", not "safe" or "will keep going".

## Quick start

```bash
cd pumpfun_tracker
pip install -r requirements.txt
python run.py --simulate     # synthetic market, no network needed
python run.py                # live data from PumpPortal
# open http://127.0.0.1:8080
```

Optional alert channels (environment variables):

| Variable | Purpose |
|---|---|
| `DISCORD_WEBHOOK_URL` | Post alerts to a Discord channel |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Send alerts to a Telegram chat |

Tuning flags: `--alert-score 70 --min-change 0.5 --min-buyers 10 --max-tokens 1500`.

The server binds to `127.0.0.1` by default and has no authentication. Put it
behind a reverse proxy with auth before using `--host 0.0.0.0`.

## Architecture

```
 PumpPortal websocket ──► Feed ──► Engine ──► Scorer (every 1s)
 (or SimulatedFeed)        │         │  ├─► alerts ─► console / Discord / Telegram
   subscribeNewToken       │         │  └─► prune idle tokens ─► unsubscribe
   subscribeMigration      │         ▼
   subscribeTokenTrade ◄───┘   aiohttp server
   (per tracked mint)          ├─ GET /api/tokens?sort=score|change_5m|mcap|new|progress
                               ├─ GET /api/tokens/{mint}   (chart history, recent trades)
                               ├─ GET /api/alerts
                               ├─ GET /api/stream          (Server-Sent Events: snapshots + alerts)
                               └─ GET /                    (dashboard)
```

| File | Role |
|---|---|
| `tracker/feeds.py` | `PumpPortalFeed`: one websocket for all streams, auto-reconnect with backoff, re-subscribes tracked mints. `SimulatedFeed`: synthetic launches using the real bonding-curve math. |
| `tracker/engine.py` | Parses messages into per-token state, rescores, applies alert rules, prunes. |
| `tracker/models.py` | `TokenState` (curve reserves, rolling trade window, peak, dev-sold flag) and curve constants. |
| `tracker/scoring.py` | Momentum score and risk flags. |
| `tracker/alerts.py` | Alert formatting and delivery. |
| `tracker/server.py` | HTTP API, SSE broadcast, background loops (tick, SOL/USD price). |
| `static/index.html` | Single-file dashboard: live table, filters, token detail with chart, alert feed. |

## Scoring

Each token gets a 0–100 score once per second. The score is a weighted sum of
features that each level off toward 1, so a single outlier can't dominate.
That sum is then multiplied by risk penalties.

| Feature | Weight | Reaches 0.63 at |
|---|---|---|
| Market-cap change, 5 min | 25% | +100% |
| Distinct buyers, 5 min | 20% | 25 wallets |
| Market-cap change, 1 min | 15% | +30% |
| Net SOL inflow, 1 min | 15% | +5 SOL |
| Trades per minute | 10% | 30 |
| Buy pressure (buy-count ratio above 50%) | 10% | linear |
| Bonding-curve progress | 5% | linear |

| Penalty | Multiplier | Flag |
|---|---|---|
| Creator sold any tokens | ×0.6 | `dev_sold` |
| One wallet supplies over half of 5-min buy volume | ×(1.5 − share) | `whale_driven` |
| Fewer than 5 distinct buyers | ×0.5 | `thin` |
| More than 30% below peak | ×0.7 | `dumping` |

Info flags: `near_graduation` (curve ≥ 90%) and `graduated` (migrated off the curve).

**A "skyrocketing" alert fires** when all of these are true:
- score ≥ 70
- market cap is up at least 50% over 5 minutes
- at least 10 distinct buyers in 5 minutes

Those conditions must **hold for 30 seconds**. A token isn't re-alerted for 10 minutes.

The 30-second hold matters. In simulation it removed every alert on
"pump then dev dumps" tokens and still caught about 90% of genuine runners.
That is a check against my own simulator, though, so tune the thresholds on
live data before trusting them.

Bonding-curve progress is computed from `vTokensInBondingCurve`. pump.fun starts
at 1,073,000,000 virtual tokens and graduates after 793,100,000 have been sold.

## Tests

```bash
python -m pytest -q tests
```

The tests cover token creation, an organic pump (alerts only after the hold
window, then the cooldown applies), a whale-only pump, a dev dump, a brief
spike, curve progress and migration, pruning and the token cap, and an
end-to-end simulated run.

## Ideas for next steps

- Persist trades to SQLite or Parquet, then backtest the weights against what
  happened next (for example, did the token reach 2× within 30 minutes of the alert?).
- Holder concentration from the `newTokenBalance` field of each trade.
- Creator reputation: track wallets whose earlier launches rugged.
- Fetch token metadata from `uri` (image, socials) and add a "has socials" signal.
- Keep tracking graduated tokens on PumpSwap/Raydium.
