"""Alerts to your phone: an urgent alarm-style push (ntfy app), SMS and phone calls (Twilio).

Real Amber Alerts / Wireless Emergency Alerts are government-only. The closest
an app can get:
  * ntfy at max priority: long vibration bursts and a pop-over notification.
    On Android the channel can be allowed to override Do Not Disturb.
  * A Twilio phone call that reads the alert out loud. Save the Twilio number
    as a contact with Emergency Bypass (iPhone) or as a starred contact allowed
    through Do Not Disturb (Android), and it rings even on silent.

One gate decides which alerts reach your phone at all (minimum score plus an
hourly cap), so a hot market can't spam you or run up a Twilio bill.
"""
import base64
import logging
import re
import time
from collections import deque
from typing import Optional
from xml.sax.saxutils import escape

import aiohttp

log = logging.getLogger(__name__)

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
TOPIC = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TWILIO_API = "https://api.twilio.com"
NTFY_SERVER = "https://ntfy.sh"


def valid_number(n: str) -> bool:
    return bool(E164.match(n or ""))


def valid_topic(t: str) -> bool:
    return bool(TOPIC.match(t or ""))


def _ascii_symbol(alert: dict) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", alert["symbol"])[:12] or "UNKNOWN"


def format_sms(alert: dict, suppressed: int = 0) -> str:
    # Plain ASCII on purpose: one emoji switches the whole SMS to UCS-2, which
    # cuts each segment from 160 to 70 characters and costs more.
    text = (f"ROCKET ${_ascii_symbol(alert)} score {alert['score']:.0f}, +{alert['change_5m'] * 100:.0f}% in 5m, "
            f"mcap {alert['mcap_sol']:.0f} SOL, {alert['buyers_5m']} buyers")
    if alert.get("flags"):
        text += f" [{','.join(alert['flags'])}]"
    text += f"\npump.fun/coin/{alert['mint']}"
    if suppressed:
        text += f"\n(+{suppressed} more alerts not sent: hourly limit)"
    return text


def format_speech(alert: dict) -> str:
    spelled = " ".join(_ascii_symbol(alert))  # "P E P E" is clearer than guessing a pronunciation
    return (f"Rocket Radar alert. {spelled} is skyrocketing. Score {alert['score']:.0f}. "
            f"Up {alert['change_5m'] * 100:.0f} percent in five minutes. Check your text messages or the dashboard.")


class AlertGate:
    """Minimum score + max alerts per hour. Remembers how many were held back."""

    def __init__(self, min_score: float = 80.0, max_per_hour: int = 6, clock=time.time):
        self.min_score = min_score
        self.max_per_hour = max_per_hour
        self.clock = clock
        self.sent_times: deque = deque()
        self.suppressed = 0

    def admit(self, alert: dict) -> Optional[int]:
        """None if the alert shouldn't go out; otherwise how many earlier alerts were held back."""
        if alert["score"] < self.min_score:
            return None
        now = self.clock()
        while self.sent_times and self.sent_times[0] < now - 3600:
            self.sent_times.popleft()
        if len(self.sent_times) >= self.max_per_hour:
            self.suppressed += 1
            log.info("phone alert hourly limit reached; holding back $%s", alert["symbol"])
            return None
        self.sent_times.append(now)
        held, self.suppressed = self.suppressed, 0
        return held


class _Http:
    def __init__(self):
        self.session = None

    def http(self) -> aiohttp.ClientSession:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None


class NtfyChannel(_Http):
    """Urgent push notification through the free ntfy app (ntfy.sh or a self-hosted server)."""

    def __init__(self, topic: str, server: str = NTFY_SERVER):
        super().__init__()
        self.topic = topic
        self.server = server.rstrip("/")
        self.label = f"phone alarm (ntfy topic {topic})"

    async def _post(self, title: str, body: str, click: str = "") -> tuple:
        headers = {"Title": title, "Priority": "5", "Tags": "rotating_light,rocket"}
        if click:
            headers["Click"] = click
        try:
            async with self.http().post(f"{self.server}/{self.topic}", data=body.encode(), headers=headers) as r:
                return (r.status < 300, None if r.status < 300 else f"HTTP {r.status}")
        except Exception as e:
            return (False, str(e))

    async def send(self, alert: dict, suppressed: int) -> list:
        ok, err = await self._post(f"ROCKET ${_ascii_symbol(alert)} score {alert['score']:.0f}",
                                   format_sms(alert, suppressed), f"https://pump.fun/coin/{alert['mint']}")
        if not ok:
            log.warning("ntfy alert failed: %s", err)
        return [(self.label, ok, err)]

    async def test(self) -> list:
        ok, err = await self._post("Rocket Radar test", "Alerts are working. This is what a rocket alert looks like.")
        return [(self.label, ok, err)]


class TwilioChannel(_Http):
    """SMS and/or a spoken phone call through Twilio."""

    def __init__(self, account_sid: str, auth_token: str, from_number: str, to_numbers: list,
                 sms: bool = True, call: bool = False, api_base: str = TWILIO_API):
        super().__init__()
        self.account_sid = account_sid
        self.from_number = from_number
        self.to_numbers = list(to_numbers)
        self.sms, self.call = sms, call
        self.base = f"{api_base.rstrip('/')}/2010-04-01/Accounts/{account_sid}"
        cred = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        self.headers = {"Authorization": f"Basic {cred}"}
        kinds = [k for k, on in (("SMS", sms), ("phone call", call)) if on]
        self.label = f"{' + '.join(kinds)} to {', '.join(self.to_numbers)}"

    async def _post(self, resource: str, data: dict) -> tuple:
        try:
            async with self.http().post(f"{self.base}/{resource}.json", headers=self.headers, data=data) as r:
                if r.status < 300:
                    return True, None
                try:
                    return False, (await r.json()).get("message", f"HTTP {r.status}")
                except Exception:
                    return False, f"HTTP {r.status}"
        except Exception as e:
            return False, str(e)

    async def _deliver(self, text: Optional[str], speech: Optional[str]) -> list:
        results = []
        for to in self.to_numbers:
            if self.sms and text:
                ok, err = await self._post("Messages", {"From": self.from_number, "To": to, "Body": text})
                results.append((f"SMS {to}", ok, err))
            if self.call and speech:
                twiml = f'<Response><Say loop="2">{escape(speech)}</Say></Response>'
                ok, err = await self._post("Calls", {"From": self.from_number, "To": to, "Twiml": twiml})
                results.append((f"call {to}", ok, err))
        for label, ok, err in results:
            if not ok:
                log.warning("%s failed: %s", label, err)
        return results

    async def send(self, alert: dict, suppressed: int) -> list:
        return await self._deliver(format_sms(alert, suppressed), format_speech(alert))

    async def test(self) -> list:
        return await self._deliver("Rocket Radar test: SMS alerts are working.",
                                   "This is a Rocket Radar test call. Phone call alerts are working.")


class PhoneAlerts:
    def __init__(self, gate: AlertGate, channels: list):
        self.gate = gate
        self.channels = channels

    def describe(self) -> str:
        return (f"{'; '.join(c.label for c in self.channels)} - alerts scoring {self.gate.min_score:.0f}+, "
                f"max {self.gate.max_per_hour}/hour")

    async def send_alert(self, alert: dict) -> bool:
        held = self.gate.admit(alert)
        if held is None:
            return False
        results = []
        for c in self.channels:
            results += await c.send(alert, held)
        return any(ok for _, ok, _ in results)

    async def send_test(self) -> list:
        results = []
        for c in self.channels:
            results += await c.test()
        return results

    async def close(self) -> None:
        for c in self.channels:
            await c.close()
