"""Momentum score: how hard is this token ripping right now, and how organic does it look?

The score (0-100) is a weighted blend of saturating features, multiplied by
risk penalties. Saturation keeps one huge number (e.g. a single 50 SOL buy)
from dominating; penalties catch the classic fakes (dev dumping, one wallet
doing all the buying, a pump that has already rolled over).
"""
import math
from collections import defaultdict

from .models import TokenState

WEIGHTS = {
    "change_5m": 0.25,
    "change_1m": 0.15,
    "net_flow_1m": 0.15,
    "buyers_5m": 0.20,
    "trades_per_min": 0.10,
    "buy_pressure": 0.10,
    "progress": 0.05,
}


def saturate(x: float, scale: float) -> float:
    """Map [0, inf) onto [0, 1); x == scale gives ~0.63."""
    return 1.0 - math.exp(-x / scale) if x > 0 else 0.0


def compute_signals(token: TokenState, now: float) -> dict:
    w5 = token.recent(now - 300)
    w1 = [t for t in w5 if t.ts >= now - 60]

    base_1m = token.mcap_at(now - 60)
    base_5m = token.mcap_at(now - 300)
    buys_5m = [t for t in w5 if t.is_buy]

    by_wallet = defaultdict(float)
    for t in buys_5m:
        by_wallet[t.trader] += t.sol
    buy_sol_5m = sum(by_wallet.values())

    return {
        "change_1m": token.mcap_sol / base_1m - 1 if base_1m else 0.0,
        "change_5m": token.mcap_sol / base_5m - 1 if base_5m else 0.0,
        "net_flow_1m": sum(t.sol if t.is_buy else -t.sol for t in w1),
        "buy_sol_5m": buy_sol_5m,
        "buyers_5m": len(by_wallet),
        "trades_1m": len(w1),
        "buy_ratio_5m": len(buys_5m) / len(w5) if w5 else 0.0,
        "top_buyer_share": max(by_wallet.values()) / buy_sol_5m if buy_sol_5m else 0.0,
        "drawdown": 1 - token.mcap_sol / token.peak_mcap_sol if token.peak_mcap_sol else 0.0,
    }


def score_token(token: TokenState, now: float) -> None:
    s = compute_signals(token, now)

    parts = {
        "change_5m": saturate(s["change_5m"], 1.0),        # +100% in 5m -> 0.63
        "change_1m": saturate(s["change_1m"], 0.3),        # +30% in 1m  -> 0.63
        "net_flow_1m": saturate(s["net_flow_1m"], 5.0),    # +5 SOL net  -> 0.63
        "buyers_5m": saturate(s["buyers_5m"], 25),         # 25 wallets  -> 0.63
        "trades_per_min": saturate(s["trades_1m"], 30),
        "buy_pressure": min(max((s["buy_ratio_5m"] - 0.5) * 2, 0.0), 1.0),
        "progress": token.progress,
    }
    raw = sum(WEIGHTS[k] * v for k, v in parts.items())

    penalty = 1.0
    flags = []
    if token.creator_sold:
        penalty *= 0.6
        flags.append("dev_sold")
    if s["top_buyer_share"] > 0.5 and s["buy_sol_5m"] > 1.0:
        penalty *= 1.5 - s["top_buyer_share"]  # 0.5 share -> x1.0, 1.0 share -> x0.5
        flags.append("whale_driven")
    if s["buyers_5m"] < 5:
        penalty *= 0.5
        flags.append("thin")
    if s["drawdown"] > 0.3:
        penalty *= 0.7
        flags.append("dumping")
    if token.progress >= 0.9 and not token.migrated:
        flags.append("near_graduation")
    if token.migrated:
        flags.append("graduated")

    token.score = 100 * raw * penalty
    token.signals = {k: round(v, 4) for k, v in s.items()}
    token.flags = flags
