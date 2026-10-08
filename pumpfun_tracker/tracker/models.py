"""In-memory state for every token being tracked."""
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# pump.fun bonding curve constants (tokens have 6 decimals; amounts here are whole tokens).
TOTAL_SUPPLY = 1_000_000_000
INITIAL_VIRTUAL_TOKENS = 1_073_000_000
INITIAL_VIRTUAL_SOL = 30.0
TOKENS_FOR_SALE = 793_100_000  # tokens sold through the curve before it graduates


@dataclass
class Trade:
    ts: float
    trader: str
    is_buy: bool
    sol: float
    tokens: float
    mcap_sol: float


@dataclass
class TokenState:
    mint: str
    name: str
    symbol: str
    creator: str
    created_at: float
    uri: str = ""
    mcap_sol: float = INITIAL_VIRTUAL_SOL / INITIAL_VIRTUAL_TOKENS * TOTAL_SUPPLY
    initial_mcap_sol: float = INITIAL_VIRTUAL_SOL / INITIAL_VIRTUAL_TOKENS * TOTAL_SUPPLY
    v_tokens: float = INITIAL_VIRTUAL_TOKENS
    v_sol: float = INITIAL_VIRTUAL_SOL
    peak_mcap_sol: float = 0.0
    last_trade_at: float = 0.0
    trade_count: int = 0
    creator_sold: bool = False
    migrated: bool = False
    trades: deque = field(default_factory=deque)
    balances: dict = field(default_factory=dict)

    # Filled in by the scorer on every tick
    score: float = 0.0
    signals: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    hot_since: Optional[float] = None    # when alert conditions started holding
    last_alert_at: Optional[float] = None

    @property
    def progress(self) -> float:
        """Share of the bonding curve that has been bought (1.0 = graduates to an AMM)."""
        if self.migrated:
            return 1.0
        p = (INITIAL_VIRTUAL_TOKENS - self.v_tokens) / TOKENS_FOR_SALE
        return min(max(p, 0.0), 1.0)

    def add_trade(self, trade: Trade, history_s: float) -> None:
        self.trades.append(trade)
        self.trade_count += 1
        self.last_trade_at = trade.ts
        self.mcap_sol = trade.mcap_sol
        self.peak_mcap_sol = max(self.peak_mcap_sol, trade.mcap_sol)
        if not trade.is_buy and trade.trader == self.creator:
            self.creator_sold = True
        cutoff = trade.ts - history_s
        while self.trades and self.trades[0].ts < cutoff:
            self.trades.popleft()

    def mcap_at(self, ts: float) -> float:
        """Market cap as of `ts` (the last trade at or before it, else the launch price)."""
        mcap = self.initial_mcap_sol
        for t in self.trades:
            if t.ts > ts:
                break
            mcap = t.mcap_sol
        return mcap

    def summary(self, now: float, sol_usd: Optional[float] = None) -> dict:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "creator": self.creator,
            "uri": self.uri,
            "age_s": round(now - self.created_at, 1),
            "mcap_sol": round(self.mcap_sol, 3),
            "mcap_usd": round(self.mcap_sol * sol_usd) if sol_usd else None,
            "peak_mcap_sol": round(self.peak_mcap_sol, 3),
            "progress": round(self.progress, 4),
            "migrated": self.migrated,
            "trade_count": self.trade_count,
            "score": round(self.score, 1),
            "signals": self.signals,
            "flags": self.flags,
            "last_alert_at": self.last_alert_at,
        }

    def history(self, points: int = 120) -> list:
        """Downsampled [ts, mcap] series for charts."""
        trades = list(self.trades)
        if len(trades) > points:
            step = len(trades) / points
            trades = [trades[int(i * step)] for i in range(points)] + [trades[-1]]
        return [[round(t.ts, 2), round(t.mcap_sol, 3)] for t in trades]
