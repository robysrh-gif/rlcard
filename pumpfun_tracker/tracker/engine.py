"""Turns raw feed messages into token state, scores, and alerts."""
import logging
import math
import time
from collections import deque
from typing import Callable, Optional

from .config import Config
from .models import TokenState, Trade
from .scoring import score_token

log = logging.getLogger(__name__)


def _num(msg: dict, key: str, optional: bool = False) -> Optional[float]:
    v = msg.get(key)
    if v is None:
        if optional:
            return None
        v = 0
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{key} is not a number")
    v = float(v)
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{key} is not a finite non-negative number")
    return v


def _numbers(msg: dict) -> dict:
    """Parse every numeric field up front so a bad message can't half-apply."""
    out = {
        "sol": _num(msg, "solAmount"),
        "tokens": _num(msg, "tokenAmount", True) or _num(msg, "initialBuy"),
        "v_tokens": _num(msg, "vTokensInBondingCurve", True),
        "v_sol": _num(msg, "vSolInBondingCurve", True),
        "mcap": _num(msg, "marketCapSol", True),
    }
    if out["mcap"] == 0:
        raise ValueError("marketCapSol is zero")
    return out


def _text(v, limit: int) -> str:
    return v[:limit] if isinstance(v, str) else ""


def _safe_uri(v) -> str:
    uri = _text(v, 300)
    return uri if uri.startswith(("https://", "ipfs://")) else ""


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
        self.phone_info: Optional[dict] = None  # shown on the dashboard (e.g. ntfy topic to subscribe to)
        self.pinned: set[str] = set()  # mints that must not be pruned (e.g. open paper positions)

    # ---- ingestion -----------------------------------------------------

    def handle(self, msg) -> Optional[str]:
        """Process one PumpPortal message. Returns a mint to subscribe to, if any.

        Malformed messages are dropped without touching any state.
        """
        if not isinstance(msg, dict) or not isinstance(msg.get("mint"), str) or not msg["mint"]:
            return None
        tx = msg.get("txType")
        try:
            if tx == "create":
                return self._on_create(msg)
            if tx in ("buy", "sell"):
                self._on_trade(msg)
            elif tx == "migrate":
                token = self.tokens.get(msg["mint"])
                if token:
                    token.migrated = True
        except ValueError as e:
            log.debug("dropping malformed %s message: %s", tx, e)
        return None

    def _on_create(self, msg: dict) -> Optional[str]:
        mint = msg["mint"]
        if mint in self.tokens:
            return None
        nums = _numbers(msg)  # validate before creating anything
        now = self.clock()
        token = TokenState(
            mint=mint,
            name=_text(msg.get("name"), 64),
            symbol=_text(msg.get("symbol"), 16),
            creator=_text(msg.get("traderPublicKey"), 64),
            created_at=now,
            uri=_safe_uri(msg.get("uri")),
        )
        self.tokens[mint] = token
        self.created_total += 1
        # The create transaction usually bundles the dev's initial buy.
        if nums["sol"] > 0:
            self._apply_trade(token, msg, nums, is_buy=True, now=now)
        else:
            self._apply_curve(token, nums)
            token.peak_mcap_sol = token.mcap_sol
        return mint

    def _on_trade(self, msg: dict) -> None:
        token = self.tokens.get(msg["mint"])
        if token:
            self._apply_trade(token, msg, _numbers(msg), is_buy=msg["txType"] == "buy", now=self.clock())

    @staticmethod
    def _apply_curve(token: TokenState, nums: dict) -> None:
        if nums["v_tokens"] is not None:
            token.v_tokens = nums["v_tokens"]
        if nums["v_sol"] is not None:
            token.v_sol = nums["v_sol"]
        if nums["mcap"] is not None:
            token.mcap_sol = nums["mcap"]

    def _apply_trade(self, token: TokenState, msg: dict, nums: dict, is_buy: bool, now: float) -> None:
        self._apply_curve(token, nums)
        token.add_trade(
            Trade(
                ts=now,
                trader=_text(msg.get("traderPublicKey"), 64),
                is_buy=is_buy,
                sol=nums["sol"],
                tokens=nums["tokens"],
                mcap_sol=token.mcap_sol,
            ),
            self.config.history_s,
        )
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
            if m not in self.pinned and now - max(t.last_trade_at, t.created_at) > cfg.idle_prune_s
        ]
        overflow = len(self.tokens) - len(dead) - cfg.max_tokens
        if overflow > 0:
            dead_set = set(dead) | self.pinned
            alive = sorted(
                (t for m, t in self.tokens.items() if m not in dead_set),
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
            "phone": self.phone_info,
        }
