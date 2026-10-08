"""Turns raw feed messages into token state, scores, and alerts."""
import time
from collections import deque
from typing import Callable, Optional

from .config import Config
from .models import TokenState, Trade
from .scoring import score_token


class Engine:
    def __init__(self, config: Config, clock: Callable[[], float] = time.time):
        self.config = config
        self.clock = clock
        self.tokens: dict[str, TokenState] = {}
        self.alerts: deque = deque(maxlen=200)
        self.trade_times: deque = deque()  # for the global trades/min stat
        self.created_total = 0
        self.sol_usd: Optional[float] = None
        self.feed_status = "starting"

    # ---- ingestion -----------------------------------------------------

    def handle(self, msg: dict) -> Optional[str]:
        """Process one PumpPortal message. Returns a mint to subscribe to, if any."""
        tx = msg.get("txType")
        if tx == "create":
            return self._on_create(msg)
        if tx in ("buy", "sell"):
            self._on_trade(msg)
        elif tx == "migrate":
            token = self.tokens.get(msg.get("mint", ""))
            if token:
                token.migrated = True
        return None

    def _on_create(self, msg: dict) -> Optional[str]:
        mint = msg.get("mint")
        if not mint or mint in self.tokens:
            return None
        now = self.clock()
        token = TokenState(
            mint=mint,
            name=str(msg.get("name", ""))[:64],
            symbol=str(msg.get("symbol", ""))[:16],
            creator=msg.get("traderPublicKey", ""),
            created_at=now,
            uri=msg.get("uri", ""),
        )
        self.tokens[mint] = token
        self.created_total += 1
        # The create transaction usually bundles the dev's initial buy.
        if float(msg.get("solAmount") or 0) > 0:
            self._apply_trade(token, msg, is_buy=True, now=now)
        else:
            self._apply_curve(token, msg)
            token.peak_mcap_sol = token.mcap_sol
        return mint

    def _on_trade(self, msg: dict) -> None:
        token = self.tokens.get(msg.get("mint", ""))
        if token:
            self._apply_trade(token, msg, is_buy=msg["txType"] == "buy", now=self.clock())

    def _apply_curve(self, token: TokenState, msg: dict) -> None:
        if msg.get("vTokensInBondingCurve") is not None:
            token.v_tokens = float(msg["vTokensInBondingCurve"])
        if msg.get("vSolInBondingCurve") is not None:
            token.v_sol = float(msg["vSolInBondingCurve"])
        if msg.get("marketCapSol") is not None:
            token.mcap_sol = float(msg["marketCapSol"])

    def _apply_trade(self, token: TokenState, msg: dict, is_buy: bool, now: float) -> None:
        self._apply_curve(token, msg)
        trader = msg.get("traderPublicKey", "")
        token.add_trade(
            Trade(
                ts=now,
                trader=trader,
                is_buy=is_buy,
                sol=float(msg.get("solAmount") or 0),
                tokens=float(msg.get("tokenAmount") or msg.get("initialBuy") or 0),
                mcap_sol=token.mcap_sol,
            ),
            self.config.history_s,
        )
        if msg.get("newTokenBalance") is not None:
            token.balances[trader] = float(msg["newTokenBalance"])
        self.trade_times.append(now)

    # ---- periodic work -------------------------------------------------

    def tick(self) -> tuple[list[dict], list[str]]:
        """Rescore everything. Returns (new alerts, mints to unsubscribe)."""
        now = self.clock()
        while self.trade_times and self.trade_times[0] < now - 60:
            self.trade_times.popleft()

        new_alerts = []
        for token in self.tokens.values():
            score_token(token, now)
            if self._should_alert(token, now):
                token.last_alert_at = now
                alert = {
                    "ts": now,
                    "mint": token.mint,
                    "symbol": token.symbol,
                    "name": token.name,
                    "score": round(token.score, 1),
                    "mcap_sol": round(token.mcap_sol, 2),
                    "change_5m": token.signals["change_5m"],
                    "buyers_5m": token.signals["buyers_5m"],
                    "flags": list(token.flags),
                }
                self.alerts.appendleft(alert)
                new_alerts.append(alert)

        return new_alerts, self._prune(now)

    def _should_alert(self, token: TokenState, now: float) -> bool:
        cfg, s = self.config, token.signals
        hot = (token.score >= cfg.alert_score
               and s["change_5m"] >= cfg.alert_min_change_5m
               and s["buyers_5m"] >= cfg.alert_min_buyers)
        if not hot:
            token.hot_since = None
            return False
        if token.hot_since is None:
            token.hot_since = now
        if now - token.hot_since < cfg.alert_sustain_s:
            return False
        return token.last_alert_at is None or now - token.last_alert_at >= cfg.alert_cooldown_s

    def _prune(self, now: float) -> list[str]:
        cfg = self.config
        dead = [
            m for m, t in self.tokens.items()
            if now - max(t.last_trade_at, t.created_at) > cfg.idle_prune_s
        ]
        overflow = len(self.tokens) - len(dead) - cfg.max_tokens
        if overflow > 0:
            alive = sorted(
                (t for m, t in self.tokens.items() if m not in set(dead)),
                key=lambda t: (t.score, t.last_trade_at),
            )
            dead += [t.mint for t in alive[:overflow]]
        for m in dead:
            del self.tokens[m]
        return dead

    # ---- views ---------------------------------------------------------

    def ranked(self, limit: int = 100, sort: str = "score") -> list[dict]:
        now = self.clock()
        keys = {
            "score": lambda t: t.score,
            "change_5m": lambda t: t.signals.get("change_5m", 0),
            "mcap": lambda t: t.mcap_sol,
            "new": lambda t: t.created_at,
            "progress": lambda t: t.progress,
        }
        tokens = sorted(self.tokens.values(), key=keys.get(sort, keys["score"]), reverse=True)
        return [t.summary(now, self.sol_usd) for t in tokens[:limit]]

    def stats(self) -> dict:
        return {
            "tracked": len(self.tokens),
            "created_total": self.created_total,
            "trades_per_min": len(self.trade_times),
            "rockets": sum(1 for t in self.tokens.values() if t.score >= self.config.alert_score),
            "sol_usd": self.sol_usd,
            "feed": self.feed_status,
            "alert_score": self.config.alert_score,
        }
