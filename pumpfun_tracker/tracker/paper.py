"""Paper trading: pretend to buy every alert and record what would have happened.

Each alert opens a simulated position after a short delay, so you don't get the
alert's price. Every fill pays slippage, the pump.fun fee and a network fee.
The position then follows fixed exit rules (take profit, stop loss, dev sold,
max hold). Separately, the token is marked every minute for an hour to show
what holding would have returned, and which minute was best to sell. Finished positions go to a JSONL
file so results add up across restarts and can be analysed later with
`python -m tracker.report`.
"""
import json
import logging
import os
import statistics
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger(__name__)
STALE_S = 120  # no trades for this long -> the price we see may not be real any more


@dataclass
class PaperConfig:
    # Defaults are deliberately pessimistic: paper results should flatter nobody.
    stake_sol: float = 0.1
    entry_delay_s: float = 5.0      # alert -> you react -> buy lands; filled at the WORST price in that window
    exit_delay_s: float = 2.0       # exit trigger -> sell lands; filled at the worst price in that window
    slippage: float = 0.03          # extra adverse move on each fill, on top of your own price impact
    fee: float = 0.0125             # pump.fun fee per side (0.95% protocol + 0.30% creator since Sept 2025)
    bot_fee: float = 0.01           # trading bot/terminal fee per side; set 0 if you trade on pump.fun directly
    tx_cost_sol: float = 0.005      # priority fee + tip per transaction to land quickly
    take_profit: float = 1.0        # exit at +100% net
    stop_loss: float = 0.3          # exit at -30% net
    exit_on_dev_sell: bool = True   # exit if the creator sells after we bought
    max_hold_s: float = 3600.0
    graduation_haircut: float = 0.30  # after graduation we can't see the price; assume we sell 30% lower
    max_open: int = 100               # cap simultaneous positions (they keep tokens subscribed)
    horizons: tuple = tuple(range(60, 3601, 60))  # mark the return at every minute for an hour
    path: str = "paper_trades.jsonl"


@dataclass
class Position:
    mint: str
    symbol: str
    name: str
    alert_ts: float
    alert_score: float
    alert_mcap: float
    alert_flags: list
    entry_ts: Optional[float] = None
    entry_mcap: Optional[float] = None    # market cap at the moment our buy lands
    dev_sold_at_entry: bool = False
    last_mcap: float = 0.0
    peak_mcap: float = 0.0
    trough_mcap: float = 0.0
    exit_ts: Optional[float] = None
    exit_mcap: Optional[float] = None
    exit_reason: Optional[str] = None
    ret: Optional[float] = None           # net return of the exit-rules strategy
    pnl_sol: Optional[float] = None
    marks: dict = field(default_factory=dict)  # horizon seconds -> net return if held
    spent: float = 0.0                    # SOL put in, including the buy transaction cost
    pending_exit: Optional[list] = None   # [reason, trigger_ts] while the sell is landing
    graduated: bool = False
    stale_from_minute: Optional[int] = None  # first minute mark taken with no trades for 2+ min
    # After graduation, remaining minute marks repeat the sold-at-graduation return.


class PaperTrader:
    def __init__(self, config: PaperConfig, engine):
        self.config = config
        self.engine = engine
        self.open: dict[str, Position] = {}
        self.closed: list[dict] = load_records(config.path)
        self.skipped = 0  # alerts ignored because max_open was reached

    # ---- returns -------------------------------------------------------

    def net_return(self, entry_mcap: float, exit_mcap: float, v_sol: float = 0.0) -> tuple[float, float]:
        """(return, pnl in SOL) for buying at entry_mcap and selling at exit_mcap, after all costs.

        `v_sol` is the curve's SOL reserve at exit; selling into it moves the price against you.
        Entry impact is already baked into the stored entry_mcap.
        """
        c = self.config
        spent = c.stake_sol + c.tx_cost_sol
        if entry_mcap <= 0:
            return -1.0, -spent
        side = 1 - c.fee - c.bot_fee
        gross = c.stake_sol * side * max(exit_mcap, 0.0) / entry_mcap
        impact = min(gross / v_sol, 0.5) if v_sol > 0 else 0.0
        # Selling dust isn't worth the transaction fee, so you can't lose more than you put in.
        received = max(gross * (1 - c.slippage - impact) * side - c.tx_cost_sol, 0.0)
        return (received - spent) / spent, received - spent

    # ---- lifecycle -----------------------------------------------------

    def on_alert(self, alert: dict) -> None:
        if alert["mint"] in self.open:
            return
        if len(self.open) >= self.config.max_open:
            self.skipped += 1
            return
        self.open[alert["mint"]] = Position(
            mint=alert["mint"], symbol=alert["symbol"], name=alert["name"], alert_ts=alert["ts"],
            alert_score=alert["score"], alert_mcap=alert["mcap_sol"], alert_flags=list(alert["flags"]),
        )
        self.engine.pinned.add(alert["mint"])

    def tick(self, now: float) -> None:
        c = self.config
        feed_ok = self.engine.feed_status in ("live", "simulated")
        for mint, p in list(self.open.items()):
            token = self.engine.tokens.get(mint)
            if token is None:  # should not happen while pinned, but never crash on it
                self._finish(p, now, "gone")
                continue
            p.last_mcap = token.mcap_sol

            if p.entry_ts is None:
                if token.migrated:  # graduated before our buy landed: no pump.fun fill possible
                    self._finish(p, now, None)
                elif feed_ok and now - p.alert_ts >= c.entry_delay_s and token.mcap_sol > 0:
                    # Worst price between the alert and our fill, plus our own buy's impact.
                    worst = max([token.mcap_sol] + [t.mcap_sol for t in token.recent(p.alert_ts)])
                    impact = c.stake_sol / token.v_sol if token.v_sol > 0 else 0.0
                    p.entry_ts, p.entry_mcap = now, worst * (1 + c.slippage + impact)
                    p.peak_mcap = p.trough_mcap = token.mcap_sol
                    p.dev_sold_at_entry = token.creator_sold
                    p.spent = c.stake_sol + c.tx_cost_sol
                continue

            if token.migrated and not p.graduated:
                # Trading moves off pump.fun and our feed loses the price. Close the
                # position pessimistically and stop taking minute marks.
                p.graduated = True
                grad_mcap = token.mcap_sol * (1 - c.graduation_haircut)
                if p.exit_ts is None:
                    self._exit(p, now, grad_mcap, "graduated", token.v_sol)
                # Fill the remaining minutes with "sold at graduation" rather than dropping
                # them, otherwise the later minutes would average only the coins that failed.
                grad_ret = round(self.net_return(p.entry_mcap, grad_mcap, token.v_sol)[0], 4)
                for h in c.horizons:
                    p.marks.setdefault(str(h), grad_ret)
                self._finish(p, now, None)
                continue
            if not feed_ok:
                continue  # no trustworthy price while the feed is down: no marks, no exits

            p.peak_mcap = max(p.peak_mcap, token.mcap_sol)
            p.trough_mcap = min(p.trough_mcap, token.mcap_sol)
            held = now - p.entry_ts
            ret, _ = self.net_return(p.entry_mcap, token.mcap_sol, token.v_sol)

            for h in c.horizons:
                if str(h) not in p.marks and held >= h:
                    p.marks[str(h)] = round(ret, 4)
                    if p.stale_from_minute is None and now - token.last_trade_at > STALE_S:
                        p.stale_from_minute = h // 60

            if p.exit_ts is None and p.pending_exit is None:
                reason = None
                if ret >= c.take_profit:
                    reason = "take_profit"
                elif ret <= -c.stop_loss:
                    reason = "stop_loss"
                elif c.exit_on_dev_sell and token.creator_sold and not p.dev_sold_at_entry:
                    reason = "dev_sold"
                elif held >= c.max_hold_s:
                    reason = "max_hold"
                if reason:
                    p.pending_exit = [reason, now]
            if p.pending_exit and now - p.pending_exit[1] >= c.exit_delay_s:
                reason, since = p.pending_exit
                worst = min([token.mcap_sol] + [t.mcap_sol for t in token.recent(since)])
                self._exit(p, now, worst, reason, token.v_sol)
                p.pending_exit = None

            if p.exit_ts is not None and len(p.marks) == len(c.horizons):
                self._finish(p, now, None)

    def _exit(self, p: Position, now: float, mcap: float, reason: str, v_sol: float = 0.0) -> None:
        p.exit_ts, p.exit_mcap, p.exit_reason = now, mcap, reason
        p.ret, p.pnl_sol = (round(x, 6) for x in self.net_return(p.entry_mcap, mcap, v_sol))

    def _finish(self, p: Position, now: float, reason: Optional[str]) -> None:
        del self.open[p.mint]
        self.engine.pinned.discard(p.mint)
        if p.entry_ts is None:
            return  # never filled
        if p.exit_ts is None:
            self._exit(p, now, p.last_mcap, reason or "gone")
        record = asdict(p)
        self.closed.append(record)
        try:
            with open(self.config.path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            log.warning("could not save paper trade: %s", e)

    # ---- views ---------------------------------------------------------

    def view(self, now: float) -> dict:
        open_rows = []
        for p in self.open.values():
            ret = self.net_return(p.entry_mcap, p.last_mcap)[0] if p.entry_mcap else None
            status = "filling" if p.entry_ts is None else (p.exit_reason or "holding")
            if p.pending_exit:
                status = f"selling ({p.pending_exit[0]})"
            open_rows.append({"mint": p.mint, "symbol": p.symbol, "score": p.alert_score,
                              "age_s": round(now - p.alert_ts), "ret": ret if p.exit_ts is None else p.ret,
                              "status": status})
        recent = [{"mint": r["mint"], "symbol": r["symbol"], "score": r["alert_score"], "ret": r["ret"],
                   "status": r["exit_reason"]} for r in self.closed[-20:]][::-1]
        return {"summary": summarize(self.closed, self.config.horizons), "open": open_rows, "recent": recent,
                "settings": {"stake_sol": self.config.stake_sol, "take_profit": self.config.take_profit,
                             "stop_loss": self.config.stop_loss, "skipped": self.skipped}}


REQUIRED = ("mint", "symbol", "alert_score", "entry_mcap", "exit_reason", "ret", "pnl_sol")


def valid_record(r) -> bool:
    return (isinstance(r, dict) and all(r.get(k) is not None for k in REQUIRED)
            and isinstance(r["entry_mcap"], (int, float)) and r["entry_mcap"] > 0)


def load_records(path: str) -> list:
    """Finished trades from a JSONL file, skipping blank, truncated or malformed lines."""
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if valid_record(r):
                out.append(r)
    return out


def _spent(r: dict) -> float:
    if r.get("spent"):
        return r["spent"]
    return r["pnl_sol"] / r["ret"] if r["ret"] else 0.0  # records written before `spent` existed


def summarize(records: list, horizons=tuple(range(60, 3601, 60))) -> dict:
    """Aggregate stats over finished paper trades."""
    if not records:
        return {"trades": 0}
    rets = [r["ret"] for r in records]
    staked = sum(_spent(r) for r in records)
    out = {
        "trades": len(records),
        "win_rate": sum(x > 0 for x in rets) / len(rets),
        "avg_ret": statistics.fmean(rets),
        "median_ret": statistics.median(rets),
        "best": max(rets),
        "worst": min(rets),
        "pnl_sol": sum(r["pnl_sol"] for r in records),
        "staked": staked,
        "roi": sum(r["pnl_sol"] for r in records) / staked if staked else 0.0,
        "exits": {},
        "hold": {},
        "by_score": [],
        "avg_peak_gain": statistics.fmean(r.get("peak_mcap", r["entry_mcap"]) / r["entry_mcap"] - 1
                                          for r in records),
    }
    out["stale"] = sum(1 for r in records if r.get("stale_from_minute") is not None)
    out["graduated"] = sum(1 for r in records if r.get("graduated"))
    for r in records:
        out["exits"][r["exit_reason"]] = out["exits"].get(r["exit_reason"], 0) + 1
    for h in horizons:
        marks = [r["marks"][str(h)] for r in records if str(h) in (r.get("marks") or {})]
        if marks:
            out["hold"][str(h)] = {"avg_ret": statistics.fmean(marks),
                                   "win_rate": sum(x > 0 for x in marks) / len(marks), "n": len(marks)}
    if out["hold"]:
        best = max(out["hold"], key=lambda h: out["hold"][h]["avg_ret"])
        out["best_minute"] = {"minute": int(best) // 60, **out["hold"][best]}
    for lo, hi in ((0, 70), (70, 80), (80, 90), (90, 101)):
        bucket = [r["ret"] for r in records if lo <= r["alert_score"] < hi]
        if bucket:
            out["by_score"].append({"range": f"{lo}-{min(hi, 100)}", "n": len(bucket),
                                    "win_rate": sum(x > 0 for x in bucket) / len(bucket),
                                    "avg_ret": statistics.fmean(bucket)})
    return out
