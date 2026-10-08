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
from tracker.paper import PaperConfig, PaperTrader
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
    g = p.add_argument_group("paper trading (on by default; simulated buys of every alert)")
    g.add_argument("--no-paper", action="store_true", help="disable paper trading")
    g.add_argument("--paper-file", default=PaperConfig.path)
    g.add_argument("--stake", type=float, default=PaperConfig.stake_sol, help="SOL per paper trade")
    g.add_argument("--take-profit", type=float, default=PaperConfig.take_profit, help="1.0 = sell at +100%%")
    g.add_argument("--stop-loss", type=float, default=PaperConfig.stop_loss, help="0.3 = sell at -30%%")
    g.add_argument("--entry-delay", type=float, default=PaperConfig.entry_delay_s,
                   help="seconds from alert to your buy landing")
    g.add_argument("--slippage", type=float, default=PaperConfig.slippage, help="per fill, 0.03 = 3%%")
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
    paper = None if args.no_paper else PaperTrader(PaperConfig(
        stake_sol=args.stake, take_profit=args.take_profit, stop_loss=args.stop_loss,
        entry_delay_s=args.entry_delay, slippage=args.slippage,
        path=args.paper_file if not args.simulate or args.paper_file != PaperConfig.path
        else "paper_trades.simulated.jsonl",  # keep simulated results out of the real log
    ), engine)
    app = create_app(engine, feed, alerts, fetch_sol_price=not (args.simulate or args.no_sol_price), paper=paper)
    print(f"Dashboard: http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
