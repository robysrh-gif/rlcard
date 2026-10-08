"""Rocket Radar: a standalone pump.fun tracker that alerts your phone when a coin skyrockets.

Double-click the app (or run `python app.py`). On first launch a short setup
picks how your phone is alerted (free alarm-style push, SMS, and/or a phone
call), sends a test alert, then opens the dashboard.

    RocketRadar                run (live pump.fun data)
    RocketRadar --setup        change phone alert settings
    RocketRadar --test-alert   send a test alert to your phone and exit
    RocketRadar --report      print the paper-trading report and exit
    RocketRadar --simulate    run on simulated data (no texts are sent)
"""
import argparse
import asyncio
import getpass
import json
import logging
import os
import secrets
import sys
import webbrowser
from pathlib import Path

from aiohttp import web

from tracker import report
from tracker.alerts import AlertDispatcher
from tracker.config import Config
from tracker.engine import Engine
from tracker.feeds import PumpPortalFeed, SimulatedFeed
from tracker.paper import PaperConfig, PaperTrader
from tracker.server import create_app
from tracker.phone import AlertGate, NtfyChannel, PhoneAlerts, TwilioChannel, valid_number, valid_topic

APP_NAME = "RocketRadar"
FROZEN = getattr(sys, "frozen", False)  # running as a PyInstaller-built app


def data_dir() -> Path:
    """Per-user folder for settings and paper-trading results."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_settings(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(path: Path, settings: dict) -> None:
    path.write_text(json.dumps(settings, indent=2))
    try:
        os.chmod(path, 0o600)  # it holds your Twilio auth token
    except OSError:
        pass


# ---- setup wizard ----------------------------------------------------------

def ask(prompt: str, default=None, check=None, *, secret=False, error="Invalid value, try again."):
    suffix = f" [{default}]" if default not in (None, "") and not secret else ""
    while True:
        read = getpass.getpass if secret else input
        value = read(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            value = str(default)
        if check is None or check(value):
            return value
        print("  " + error)


def yes(prompt: str, default: bool) -> bool:
    return ask(f"{prompt} (y/n)", "y" if default else "n", lambda v: v.lower()[:1] in ("y", "n")).lower() == "y"


def setup_wizard(path: Path, current: dict) -> dict:
    s = dict(current)
    print("\n=== Rocket Radar phone alert setup ===")
    print("Pick how your phone should be alerted when a coin skyrockets. You can turn on several.\n")
    print("1) PHONE ALARM (free, recommended): an urgent pop-up with long vibration via the")
    print("   free ntfy app. No account needed. On Android it can break through Do Not Disturb.")
    print("2) SMS text message via Twilio (about $0.01 per text, Twilio account needed).")
    print("3) PHONE CALL via Twilio that reads the alert aloud (about $0.02 per call). Save the")
    print("   number as an emergency-bypass contact and it rings even on silent.\n")

    if yes("Turn on the phone alarm (1)?", bool(s.get("ntfy_topic", True))):
        topic = s.get("ntfy_topic") or "rocketradar-" + secrets.token_hex(8)
        s["ntfy_topic"] = ask("ntfy topic (keep the random one so strangers can't guess it)", topic,
                              valid_topic, error="8-64 letters, digits, - or _.")
        print(f"""
  On your phone:
    a) Install "ntfy" from the App Store or Google Play.
    b) Tap +, enter topic:  {s['ntfy_topic']}   then Subscribe.
    c) Android, for alarm-like behaviour: open the subscription's notification settings, set the
       "Max priority" channel to an alarm sound and allow it to override Do Not Disturb.
""")
    else:
        s["ntfy_topic"] = ""

    s["sms_enabled"] = yes("Turn on SMS texts (2)?", s.get("sms_enabled", False))
    s["call_enabled"] = yes("Turn on phone calls (3)?", s.get("call_enabled", False))
    if s["sms_enabled"] or s["call_enabled"]:
        print("\n  Twilio: sign up at https://www.twilio.com/try-twilio, get a phone number, and copy")
        print("  the Account SID and Auth Token from the Console home page. Trial accounts can only")
        print("  text/call numbers you've verified in Twilio.\n")
        s["twilio_account_sid"] = ask("Twilio Account SID (starts with AC)", s.get("twilio_account_sid", ""),
                                      lambda v: v.startswith("AC") and len(v) == 34,
                                      error="An Account SID starts with AC and is 34 characters long.")
        has_token = bool(s.get("twilio_auth_token"))
        s["twilio_auth_token"] = ask("Twilio Auth Token" + (" (Enter keeps the saved one)" if has_token else ""),
                                     s.get("twilio_auth_token") if has_token else None,
                                     lambda v: len(v) >= 32, error="The Auth Token is 32 characters.", secret=True)
        hint = "in +countrycode format, e.g. +15551234567"
        s["twilio_from"] = ask(f"Your Twilio phone number ({hint})", s.get("twilio_from", ""),
                               valid_number, error=f"Use {hint}.")
        to = ask("Your mobile number(s), comma-separated", ",".join(s.get("phone_to", [])),
                 lambda v: bool(v) and all(valid_number(n.strip()) for n in v.split(",")), error=f"Use {hint}.")
        s["phone_to"] = [n.strip() for n in to.split(",")]
        if s["call_enabled"]:
            print(f"\n  To make calls ring on silent: save {s['twilio_from']} as a contact named Rocket Radar.")
            print("  iPhone: contact > Edit > Ringtone > Emergency Bypass on.")
            print("  Android: star the contact, then allow starred contacts' calls in Do Not Disturb.\n")

    s["alert_min_score"] = float(ask("Only alert for scores of at least (70-100)", s.get("alert_min_score", 80),
                                     lambda v: v.replace(".", "", 1).isdigit() and 0 <= float(v) <= 100))
    s["alert_max_per_hour"] = int(ask("Maximum phone alerts per hour", s.get("alert_max_per_hour", 6),
                                      lambda v: v.isdigit() and int(v) > 0))
    save_settings(path, s)
    print(f"Saved to {path}")
    phone = make_phone(s)
    if phone and yes("Send a test alert now?", True):
        asyncio.run(send_test(phone))
    elif not phone:
        print("No phone alerts are on. Run with --setup any time to turn them on.")
    return s


def make_phone(settings: dict):
    """PhoneAlerts from saved settings; environment variables override them (handy on servers)."""
    env = os.environ
    get = lambda key, setting, default=None: env.get(key) or settings.get(setting, default)  # noqa: E731
    channels = []
    topic = get("NTFY_TOPIC", "ntfy_topic")
    if topic:
        channels.append(NtfyChannel(topic, get("NTFY_SERVER", "ntfy_server", "https://ntfy.sh")))
    sms_on = bool(env.get("SMS_ENABLED") or settings.get("sms_enabled"))
    call_on = bool(env.get("CALL_ENABLED") or settings.get("call_enabled"))
    to = [n.strip() for n in env["PHONE_TO"].split(",")] if env.get("PHONE_TO") else settings.get("phone_to", [])
    sid, token, from_number = (get("TWILIO_ACCOUNT_SID", "twilio_account_sid"),
                               get("TWILIO_AUTH_TOKEN", "twilio_auth_token"), get("TWILIO_FROM", "twilio_from"))
    if (sms_on or call_on) and sid and token and from_number and to:
        channels.append(TwilioChannel(sid, token, from_number, to, sms=sms_on, call=call_on,
                                      api_base=env.get("TWILIO_API_BASE", "https://api.twilio.com")))
    if not channels:
        return None
    gate = AlertGate(min_score=float(get("ALERT_MIN_SCORE", "alert_min_score", 80)),
                     max_per_hour=int(get("ALERT_MAX_PER_HOUR", "alert_max_per_hour", 6)))
    return PhoneAlerts(gate, channels)


async def send_test(phone) -> bool:
    results = await phone.send_test()
    await phone.close()
    for label, ok, err in results:
        print(f"  {label}: {'sent' if ok else 'FAILED - ' + str(err)}")
    if not all(ok for _, ok, _ in results):
        print("  Fix the failing one with --setup. Twilio trial accounts can only reach verified numbers.")
    return all(ok for _, ok, _ in results)


# ---- main ------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(prog=APP_NAME, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--setup", action="store_true", help="change phone alert settings")
    p.add_argument("--test-alert", action="store_true", help="send a test alert to your phone and exit")
    p.add_argument("--report", action="store_true", help="print the paper-trading report and exit")
    p.add_argument("--simulate", action="store_true", help="simulated market data; no phone alerts are sent")
    p.add_argument("--no-phone", action="store_true", help="don't send phone alerts this run")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    p.add_argument("--port", type=int, default=Config.port)
    args = p.parse_args()

    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8 (emoji in log lines)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    folder = data_dir()
    settings_path = folder / "settings.json"
    paper_path = folder / ("paper_trades.simulated.jsonl" if args.simulate else "paper_trades.jsonl")

    if args.report:
        report.main(str(paper_path))
        return 0

    settings = load_settings(settings_path)
    if args.setup:
        settings = setup_wizard(settings_path, settings)
    elif not settings_path.exists():
        # First launch: no questions. Turn on the free phone alarm with a private random topic.
        settings = {"ntfy_topic": "rocketradar-" + secrets.token_hex(8),
                    "alert_min_score": 80.0, "alert_max_per_hour": 6}
        save_settings(settings_path, settings)

    phone = None if args.no_phone or args.simulate else make_phone(settings)
    if args.test_alert:
        phone = make_phone(settings)
        if not phone:
            print("Phone alerts aren't set up. Run with --setup first.")
            return 1
        return 0 if asyncio.run(send_test(phone)) else 1

    config = Config(port=args.port)
    engine = Engine(config)
    feed = SimulatedFeed(engine) if args.simulate else PumpPortalFeed(engine)
    alerts = AlertDispatcher(
        discord_webhook=os.environ.get("DISCORD_WEBHOOK_URL"),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_chat=os.environ.get("TELEGRAM_CHAT_ID"),
        phone=phone,
    )
    topic = os.environ.get("NTFY_TOPIC") or settings.get("ntfy_topic")
    engine.phone_info = {"ntfy_topic": topic if phone and topic else None,
                         "summary": phone.describe() if phone else None}
    paper = PaperTrader(PaperConfig(path=str(paper_path)), engine)
    app = create_app(engine, feed, alerts, fetch_sol_price=not args.simulate, paper=paper)
    url = f"http://127.0.0.1:{args.port}"

    async def open_browser(_):
        if not args.no_browser:
            asyncio.get_running_loop().call_later(1.0, webbrowser.open, url)
    app.on_startup.append(open_browser)

    print(f"\nRocket Radar is running{' on SIMULATED data' if args.simulate else ''}.")
    print(f"  Dashboard: {url}")
    if args.simulate:
        print("  Phone:     off while simulating")
    else:
        print(f"  Phone:     {phone.describe() if phone else 'off (run with --setup to turn it on)'}")
        if phone and topic:
            print("\n  ONE-TIME PHONE STEP: install the free \"ntfy\" app (App Store / Google Play),")
            print(f"  tap +, and subscribe to:  {topic}")
            print("  That's it. Alerts arrive automatically. Run with --test-alert to try it.")
    print(f"  Data:      {folder}")
    print("  Keep this window open. Press Ctrl+C to stop.\n")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 0
    except Exception as e:
        logging.exception("Rocket Radar stopped with an error: %s", e)
        code = 1
    if FROZEN and code and sys.platform == "win32":
        input("Press Enter to close...")  # keep the window open so the error can be read
    sys.exit(code)
