"""Start the pump.fun rocket tracker.

    python run.py              # live data from PumpPortal
    python run.py --simulate   # synthetic feed, no network needed
"""
import argparse
import logging
import os

from aiohttp import web

from tracker.alerts import AlertDispatcher
from tracker.config import Config
from tracker.engine import Engine
from tracker.feeds import PumpPortalFeed, SimulatedFeed
from tracker.server import create_app


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--simulate", action="store_true", help="use the built-in simulated feed")
    p.add_argument("--host", default=Config.host)
    p.add_argument("--port", type=int, default=Config.port)
    p.add_argument("--alert-score", type=float, default=Config.alert_score)
    p.add_argument("--min-change", type=float, default=Config.alert_min_change_5m,
                   help="min 5-minute market cap change for an alert (0.5 = +50%%)")
    p.add_argument("--min-buyers", type=int, default=Config.alert_min_buyers)
    p.add_argument("--max-tokens", type=int, default=Config.max_tokens)
    p.add_argument("--no-sol-price", action="store_true", help="don't fetch SOL/USD from CoinGecko")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config(alert_score=args.alert_score, alert_min_change_5m=args.min_change,
                    alert_min_buyers=args.min_buyers, max_tokens=args.max_tokens,
                    host=args.host, port=args.port)
    engine = Engine(config)
    feed = SimulatedFeed(engine) if args.simulate else PumpPortalFeed(engine)
    alerts = AlertDispatcher(
        discord_webhook=os.environ.get("DISCORD_WEBHOOK_URL"),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_chat=os.environ.get("TELEGRAM_CHAT_ID"),
    )
    app = create_app(engine, feed, alerts, fetch_sol_price=not (args.simulate or args.no_sol_price))
    print(f"Dashboard: http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
