# 🚀 Rocket Radar: a pump.fun momentum tracker

Watches every new pump.fun launch in real time, scores each token on how hard
**and how organically** it is pumping, and alerts you (dashboard, browser
notification, Discord, Telegram) when one starts skyrocketing.

> Signals only, not financial advice. Most pump.fun tokens go to zero. A high
> score means "lots of broad buying right now", not "safe" or "will keep going".

## Standalone app with phone alerts (easiest)

Download a ready-made app with nothing to install. Open the repository's
**Actions** tab, then the latest **Rocket Radar app** run. Under **Artifacts**,
download the one for your computer:

| Your computer | Download | How to open it |
|---|---|---|
| Windows | `RocketRadar-Windows` | Unzip and double-click `RocketRadar.exe`. If SmartScreen warns you, click "More info", then "Run anyway". |
| Mac (M1/M2/M3/M4) | `RocketRadar-macOS-AppleSilicon` | Unzip, right-click `RocketRadar`, choose Open, then Open again. The app is unsigned. |
| Mac (Intel) | `RocketRadar-macOS-Intel` | Same as above. |
| Linux | `RocketRadar-Linux` | `chmod +x RocketRadar && ./RocketRadar` |

The first launch asks no questions. It turns on **phone alarm alerts**, sends
them to a private random topic, and opens the dashboard. Then do this once:

1. Install the free **ntfy** app from the App Store or Google Play.
2. Tap **+** and subscribe to the topic printed in the app window. The topic is
   also shown at the top of the dashboard.
3. Run `RocketRadar --test-alert` to check that alerts reach your phone.

From then on, every alert scoring 80 or more reaches your phone as an urgent,
loud notification. You get at most 6 an hour, and each one links straight to
the coin on pump.fun. The free ntfy option needs no account and costs nothing.

**SMS texts or phone calls instead of (or as well as) the alarm.** Run
`RocketRadar --setup`. Both use Twilio: about $0.01 per text and $0.02 per
call. The setup asks for your Twilio details and number, then sends a test.
The call reads the alert aloud. Save the Twilio number as a contact with
**Emergency Bypass** (iPhone) or as a starred contact allowed through Do Not
Disturb (Android), and it rings even when your phone is on silent.

> Real Amber Alerts (Wireless Emergency Alerts) are government-only. An urgent
> ntfy push plus an emergency-bypass phone call is as close as an app can get.

| Command | What it does |
|---|---|
| `RocketRadar` | Run on live pump.fun data |
| `RocketRadar --setup` | Choose phone alarm / SMS / calls, minimum score, alerts per hour |
| `RocketRadar --test-alert` | Send a test alert to your phone |
| `RocketRadar --report` | Paper-trading report |
| `RocketRadar --simulate` | Run on simulated data (no phone alerts) |

Settings and paper-trading results are kept in one folder:
- **Windows:** `%APPDATA%\RocketRadar`
- **Mac:** `~/Library/Application Support/RocketRadar`
- **Linux:** `~/.config/RocketRadar`

On a server, you can configure it with environment variables instead:
`NTFY_TOPIC`, `NTFY_SERVER`, `SMS_ENABLED=1`, `CALL_ENABLED=1`,
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`,
`PHONE_TO=+1555...,+1555...`, `ALERT_MIN_SCORE` and `ALERT_MAX_PER_HOUR`.

To build the app yourself, run `pip install pyinstaller && python build.py`.
It builds for the computer you run it on only.

## Quick start (from source)

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

## Paper trading: test it before you bet

Paper trading is **on by default**. Every alert opens a simulated 0.1 SOL
position, and nothing is actually bought. Results add up in
`paper_trades.jsonl` across restarts. Simulated-feed runs write to
`paper_trades.simulated.jsonl` instead.

```bash
python -m tracker.report            # summary of all finished paper trades
python -m tracker.report --watch    # refresh the report every minute
```

The dashboard's **Paper trading** panel shows the same numbers live, with a
bar chart of the average return if you had sold at minute 1, 2, … 60.

**Strategy rules for each paper trade.**
- **Buy:** the buy lands 5 s after the alert, at the *highest* price seen in that window.
- **Sell rules:**
  - take profit at +100% net
  - stop loss at −30% net
  - sell if the creator sells after you bought
  - sell after 60 min at the latest
- **Sell timing:** every sell lands 2 s after its trigger, at the *lowest* price in that window.
- **Graduation:** if the coin graduates, pump.fun no longer shows its price. The trade is closed at the last price minus 30%.
- **Feed outage:** while the data feed is down, no buys, sells or minute marks happen.

**Minute-by-minute check.** Separately from the rules, each trade's net return
is recorded at every minute from 1 to 60. That shows which holding time would
have worked best. After a graduation, the remaining minutes use the
sold-at-graduation return, so winners don't silently drop out of later minutes.

**Costs (deliberately pessimistic).**

| Cost | Default |
|---|---|
| pump.fun fee per side | 1.25% |
| Bot fee per side | 1% |
| Slippage per fill | 3% |
| Your own price impact | from the curve's SOL reserve |
| Network fees | 0.005 SOL per transaction |

A coin that goes nowhere loses about **16.5%** on a 0.1 SOL round trip. A 2×
nets only about **+72%**. Larger stakes dilute the fixed network fee, but your
price impact grows.

All of it can be tuned:
`--stake --take-profit --stop-loss --entry-delay --slippage --no-paper`.
For example, `--take-profit 0.5 --stop-loss 0.2` tests tighter exits.

**How to read the results.**
- Wait for **at least 50–100 finished trades**, which is a day or more of live data.
- "Best minute" and "avg peak" are hindsight. Picking the best of 60 minutes
  after the fact overstates what you'd get. Choose rules on one period's data
  and confirm them on a later period before trusting them.
- If the median trade loses money, the alerts don't work at these costs,
  whatever the average says.
- Open positions aren't saved, so a restart drops trades still in progress.
- Unverified: whether PumpPortal's free stream keeps sending prices after a
  coin graduates to PumpSwap. The tracker assumes it doesn't.
- The simulated feed only shows that the plumbing works. Its profit numbers
  mean nothing.

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
| `tracker/paper.py` | Paper trading: simulated fills with costs, exit rules, per-minute marks, JSONL log, stats. |
| `tracker/report.py` | Command-line paper-trading report (`--watch` refreshes every minute). |
| `tracker/phone.py` | Phone alerts: urgent ntfy push, Twilio SMS and voice calls, min-score and hourly cap. |
| `app.py` / `build.py` | Standalone app entry point (setup, test alert) and the PyInstaller build script. |
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
