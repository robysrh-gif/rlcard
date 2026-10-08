from dataclasses import dataclass


@dataclass
class Config:
    # Alerting
    alert_score: float = 70.0          # score needed to flag a token as skyrocketing
    alert_min_change_5m: float = 0.5   # and at least +50% market cap over 5 minutes
    alert_min_buyers: int = 10         # and at least this many distinct buyers in 5 minutes
    alert_sustain_s: float = 30.0      # conditions must hold this long (filters quick pump-and-dumps)
    alert_cooldown_s: float = 600.0    # don't re-alert the same token within this window

    # Housekeeping
    max_tokens: int = 1500             # hard cap on tokens held in memory / subscribed
    idle_prune_s: float = 900.0        # drop tokens with no trades for this long
    history_s: float = 900.0           # keep this much trade history per token

    # Server
    host: str = "127.0.0.1"
    port: int = 8080
    snapshot_interval_s: float = 1.0
