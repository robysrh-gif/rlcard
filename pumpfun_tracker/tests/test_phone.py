import asyncio
import base64
import json
import sys
from pathlib import Path

from aiohttp import web

from tracker.phone import (AlertGate, NtfyChannel, PhoneAlerts, TwilioChannel, format_sms, format_speech,
                           valid_number, valid_topic)

SID, TOKEN = "AC" + "1" * 32, "t" * 32


def alert(score=90.0, symbol="PEPE", flags=()):
    return {"mint": "Mint1111111111111111111111111111111111pump", "symbol": symbol, "name": "Pepe",
            "score": score, "mcap_sol": 150.2, "change_5m": 3.2, "buyers_5m": 61, "flags": list(flags)}


async def with_fake_services(fn, status=201, body=None):
    """Run fn(base_url, received) against local stand-ins for the Twilio and ntfy APIs."""
    received = []

    async def twilio(request):
        auth = base64.b64decode(request.headers["Authorization"].split()[1]).decode()
        received.append({"kind": request.match_info["kind"], "auth": auth, **(await request.post())})
        return web.json_response(body or {"sid": "X1"}, status=status)

    async def ntfy(request):
        received.append({"kind": "ntfy", "topic": request.match_info["topic"], "body": await request.text(),
                         **{h: request.headers.get(h) for h in ("Title", "Priority", "Click")}})
        return web.json_response({}, status=200 if status < 300 else status)

    app = web.Application()
    app.router.add_post("/2010-04-01/Accounts/{sid}/{kind}.json", twilio)
    app.router.add_post("/{topic}", ntfy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        return await fn(f"http://127.0.0.1:{port}", received)
    finally:
        await runner.cleanup()


def test_validation():
    assert valid_number("+15551234567") and not valid_number("5551234567") and not valid_number("")
    assert valid_topic("rocketradar-0123abcd") and not valid_topic("a b") and not valid_topic("short")


def test_formats_are_ascii_and_safe():
    text = format_sms(alert(symbol="P🐸PE<x>", flags=["near_graduation"]))
    assert text.isascii() and "$PPEx score 90" in text and "pump.fun/coin/Mint" in text
    assert len(text) <= 320  # at most two SMS segments
    assert format_speech(alert(symbol="<b>")) .startswith("Rocket Radar alert. b is skyrocketing")


def test_all_channels_deliver():
    async def go(base, received):
        phone = PhoneAlerts(AlertGate(), [
            NtfyChannel("rocketradar-test1234", base),
            TwilioChannel(SID, TOKEN, "+15550000000", ["+15551111111"], sms=True, call=True, api_base=base),
        ])
        assert await phone.send_alert(alert())
        await phone.close()
        return received
    received = asyncio.run(with_fake_services(go))
    push, sms, call = received
    assert push["kind"] == "ntfy" and push["topic"] == "rocketradar-test1234" and push["Priority"] == "5"
    assert push["Click"].endswith("/coin/Mint1111111111111111111111111111111111pump")
    assert sms["kind"] == "Messages" and sms["To"] == "+15551111111" and sms["auth"] == f"{SID}:{TOKEN}"
    assert call["kind"] == "Calls" and "<Say" in call["Twiml"] and "P E P E is skyrocketing" in call["Twiml"]


def test_gate_min_score_and_hourly_cap():
    clock = [1000.0]

    async def go(base, received):
        phone = PhoneAlerts(AlertGate(80, 2, clock=lambda: clock[0]), [NtfyChannel("rocketradar-test1234", base)])
        assert not await phone.send_alert(alert(score=75))
        assert await phone.send_alert(alert())
        assert await phone.send_alert(alert())
        assert not await phone.send_alert(alert())
        assert not await phone.send_alert(alert())
        clock[0] += 3601
        assert await phone.send_alert(alert())
        await phone.close()
        return received
    received = asyncio.run(with_fake_services(go))
    assert len(received) == 3 and "(+2 more alerts not sent: hourly limit)" in received[-1]["body"]


def test_twilio_error_is_reported():
    async def go(base, _):
        ch = TwilioChannel(SID, TOKEN, "+15550000000", ["+15551111111"], api_base=base)
        res = await ch.test()
        await ch.close()
        return res
    res = asyncio.run(with_fake_services(go, status=400, body={"message": "The number is unverified."}))
    assert res == [("SMS +15551111111", False, "The number is unverified.")]


def test_setup_wizard_end_to_end(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app

    answers = iter([
        "y", "",                       # phone alarm on, keep the random topic
        "y", "y",                      # SMS on, calls on
        SID, "+15550000000", "+15551111111,+15552222222",
        "85", "4",                     # min score, max per hour
        "n",                           # no test alert
    ])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(app.getpass, "getpass", lambda _: TOKEN)
    path = tmp_path / "settings.json"
    s = app.setup_wizard(path, {})
    saved = json.loads(path.read_text())
    assert saved == s and valid_topic(s["ntfy_topic"]) and s["ntfy_topic"].startswith("rocketradar-")
    assert s["phone_to"] == ["+15551111111", "+15552222222"] and s["twilio_auth_token"] == TOKEN
    phone = app.make_phone(s)
    assert [type(c).__name__ for c in phone.channels] == ["NtfyChannel", "TwilioChannel"]
    assert phone.gate.min_score == 85 and phone.gate.max_per_hour == 4
    assert phone.channels[1].sms and phone.channels[1].call
