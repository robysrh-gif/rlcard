"""Data sources. Both emit PumpPortal-shaped dicts into Engine.handle()."""
import asyncio
import json
import logging
import math
import random
import string

from .engine import Engine
from .models import INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKENS, TOTAL_SUPPLY

log = logging.getLogger(__name__)

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
SUBSCRIBE_BATCH = 100


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


class PumpPortalFeed:
    """Live feed from PumpPortal's free data websocket.

    One connection carries everything: new-token and migration streams, plus a
    trade subscription for each token we are tracking. PumpPortal asks clients
    to reuse a single connection rather than opening one per token.
    """

    def __init__(self, engine: Engine, url: str = PUMPPORTAL_URL):
        self.engine = engine
        self.url = url
        self.ws = None

    async def run(self) -> None:
        import websockets

        backoff = 1
        while True:
            try:
                self.engine.feed_status = "connecting"
                async with websockets.connect(self.url, ping_interval=20, max_size=2**22) as ws:
                    self.ws = ws
                    self.engine.feed_status = "live"
                    backoff = 1
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    # Re-subscribe to everything we were tracking before a reconnect.
                    await self._send("subscribeTokenTrade", list(self.engine.tokens))
                    async for raw in ws:
                        try:
                            msg = json.loads(raw, parse_constant=_reject_constant)
                        except ValueError:
                            continue
                        mint = self.engine.handle(msg)
                        if mint:
                            await self._send("subscribeTokenTrade", [mint])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, server closes, etc.
                log.warning("PumpPortal connection lost: %s (retrying in %ss)", e, backoff)
            self.ws = None
            self.engine.feed_status = "reconnecting"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def unsubscribe(self, mints: list) -> None:
        await self._send("unsubscribeTokenTrade", mints)

    async def _send(self, method: str, mints: list) -> None:
        if not self.ws or not mints:
            return
        for i in range(0, len(mints), SUBSCRIBE_BATCH):
            try:
                await self.ws.send(json.dumps({"method": method, "keys": mints[i:i + SUBSCRIBE_BATCH]}))
            except Exception as e:
                log.debug("send failed: %s", e)
                return


# ---------------------------------------------------------------------------
# Simulator: lets you develop, demo and test without network access.
# ---------------------------------------------------------------------------

_B58 = "".join(c for c in string.ascii_letters + string.digits if c not in "0OIl")
_WORDS = ["PEPE", "DOGE", "CAT", "MOON", "FROG", "BONK", "WIF", "CHAD", "GIGA", "TRUMP",
          "AI", "GROK", "PNUT", "MOODENG", "HAMSTER", "BASED", "COPE", "SEND", "BRRR", "TURBO"]

# archetype: (weight, buy rate/s, sell rate/s, unique-wallet pool, avg buy SOL, lifetime s)
ARCHETYPES = {
    "dud":    (0.70, 0.08, 0.06, 15, 0.3, 90),
    "rug":    (0.12, 0.60, 0.15, 40, 0.6, 120),
    "whale":  (0.08, 0.30, 0.10, 3, 3.0, 120),
    "rocket": (0.10, 1.50, 0.35, 400, 0.9, 420),
}


def _addr() -> str:
    return "".join(random.choices(_B58, k=44))


class _SimToken:
    def __init__(self, kind: str):
        self.kind = kind
        self.mint = _addr()[:40] + "pump"
        self.creator = _addr()
        word = random.choice(_WORDS)
        suffix = random.choice(["", "", "2", "INU", "COIN", "AI", "X"])
        self.symbol = (word + suffix)[:10]
        self.name = f"{word.title()} {random.choice(['Coin', 'on Sol', 'Army', 'Classic', 'Season', ''])}".strip()
        _, self.buy_rate, self.sell_rate, pool, self.avg_buy, self.lifetime = ARCHETYPES[kind]
        self.wallets = [_addr() for _ in range(pool)]
        self.holdings: dict[str, float] = {}
        self.v_sol = INITIAL_VIRTUAL_SOL
        self.v_tokens = float(INITIAL_VIRTUAL_TOKENS)
        self.age = 0.0
        self.migrated = False

    @property
    def mcap(self) -> float:
        return self.v_sol / self.v_tokens * TOTAL_SUPPLY

    def _msg(self, tx: str, trader: str, sol: float, tokens: float) -> dict:
        return {
            "signature": _addr() + _addr(),
            "mint": self.mint,
            "traderPublicKey": trader,
            "txType": tx,
            "tokenAmount": tokens,
            "solAmount": sol,
            "newTokenBalance": self.holdings.get(trader, 0.0),
            "bondingCurveKey": "sim",
            "vTokensInBondingCurve": self.v_tokens,
            "vSolInBondingCurve": self.v_sol,
            "marketCapSol": self.mcap,
            "pool": "pump",
        }

    def buy(self, trader: str, sol: float) -> dict:
        k = self.v_sol * self.v_tokens
        self.v_sol += sol
        out = self.v_tokens - k / self.v_sol
        self.v_tokens -= out
        self.holdings[trader] = self.holdings.get(trader, 0.0) + out
        return self._msg("buy", trader, sol, out)

    def sell(self, trader: str, frac: float):
        amount = self.holdings.get(trader, 0.0) * frac
        if amount <= 0:
            return None
        k = self.v_sol * self.v_tokens
        self.v_tokens += amount
        sol = self.v_sol - k / self.v_tokens
        self.v_sol -= sol
        self.holdings[trader] -= amount
        return self._msg("sell", trader, sol, amount)

    def create(self) -> dict:
        msg = self.buy(self.creator, round(random.uniform(0.2, 2.0), 3))
        msg.update(txType="create", name=self.name, symbol=self.symbol,
                   uri="", initialBuy=msg.pop("tokenAmount"))
        return msg

    def step(self, dt: float) -> list:
        self.age += dt
        if self.age > self.lifetime or self.migrated:
            return []
        out = []
        ramp = 1.0
        if self.kind == "rocket":
            ramp = 0.3 + min(self.age / 120, 2.0)  # demand keeps building
        elif self.kind == "rug" and self.age > 45 and self.creator in self.holdings:
            m = self.sell(self.creator, 1.0)
            self.holdings.pop(self.creator, None)
            self.sell_rate *= 4
            return [m] if m else []
        for _ in range(_poisson(self.buy_rate * ramp * dt)):
            out.append(self.buy(random.choice(self.wallets), random.expovariate(1 / self.avg_buy)))
        for _ in range(_poisson(self.sell_rate * dt)):
            holders = [w for w, h in self.holdings.items() if h > 0 and w != self.creator]
            if holders:
                m = self.sell(random.choice(holders), random.choice([0.25, 0.5, 1.0]))
                if m:
                    out.append(m)
        if self.v_tokens <= INITIAL_VIRTUAL_TOKENS - 793_100_000:
            self.migrated = True
            out.append({"signature": _addr(), "mint": self.mint, "txType": "migrate", "pool": "pump"})
        return out


def _poisson(lam: float) -> int:
    n, p, threshold = 0, 1.0, math.exp(-lam)
    while True:
        p *= random.random()
        if p < threshold:
            return n
        n += 1


class SimulatedFeed:
    """Generates a realistic-looking pump.fun firehose using the real curve math."""

    def __init__(self, engine: Engine, launches_per_s: float = 1.5, dt: float = 0.25, seed=None):
        self.engine = engine
        self.launches_per_s = launches_per_s
        self.dt = dt
        self.live: list[_SimToken] = []
        if seed is not None:
            random.seed(seed)

    def step(self) -> None:
        kinds, weights = zip(*((k, v[0]) for k, v in ARCHETYPES.items()))
        for _ in range(_poisson(self.launches_per_s * self.dt)):
            tok = _SimToken(random.choices(kinds, weights)[0])
            self.live.append(tok)
            self.engine.handle(tok.create())
        for tok in self.live:
            for msg in tok.step(self.dt):
                self.engine.handle(msg)
        self.live = [t for t in self.live if t.age <= t.lifetime and not t.migrated]

    async def run(self) -> None:
        self.engine.feed_status = "simulated"
        while True:
            self.step()
            await asyncio.sleep(self.dt)

    async def unsubscribe(self, mints: list) -> None:
        pass
