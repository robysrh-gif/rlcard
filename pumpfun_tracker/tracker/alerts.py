"""Where "skyrocketing" alerts go: the console, plus optional Discord / Telegram."""
import logging

import aiohttp

log = logging.getLogger(__name__)


def format_alert(a: dict) -> str:
    flags = f" [{', '.join(a['flags'])}]" if a["flags"] else ""
    return (f"🚀 ${a['symbol']} ({a['name']}) score {a['score']:.0f} | "
            f"mcap {a['mcap_sol']:.1f} SOL | +{a['change_5m'] * 100:.0f}% 5m | "
            f"{a['buyers_5m']} buyers{flags}\nhttps://gmgn.ai/sol/token/{a['mint']}\n{a['mint']}")


class AlertDispatcher:
    def __init__(self, discord_webhook: str = None, telegram_token: str = None, telegram_chat: str = None,
                 phone=None):
        self.phone = phone  # optional tracker.phone.PhoneAlerts
        self.discord_webhook = discord_webhook
        self.telegram = (telegram_token, telegram_chat) if telegram_token and telegram_chat else None
        self.session = None

    async def send(self, alert: dict) -> None:
        text = format_alert(alert)
        log.info(text.replace("\n", " "))
        if self.phone:
            await self.phone.send_alert(alert)
        if not (self.discord_webhook or self.telegram):
            return
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            if self.discord_webhook:
                async with self.session.post(self.discord_webhook, json={"content": text}) as r:
                    r.raise_for_status()
            if self.telegram:
                token, chat = self.telegram
                async with self.session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                             json={"chat_id": chat, "text": text,
                                                   "disable_web_page_preview": True}) as r:
                    r.raise_for_status()
        except Exception as e:
            log.warning("alert delivery failed: %s", e)

    async def close(self) -> None:
        if self.phone:
            await self.phone.close()
        if self.session:
            await self.session.close()
