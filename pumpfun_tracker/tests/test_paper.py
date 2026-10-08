import json

import pytest

from tracker.config import Config
from tracker.engine import Engine
from tracker.feeds import _SimToken
from tracker.paper import PaperConfig, PaperTrader, summarize


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def setup(tmp_path, **kw):
    clock = Clock()
    engine = Engine(Config(), clock=clock)
    engine.feed_status = "simulated"
    paper = PaperTrader(PaperConfig(path=str(tmp_path / "trades.jsonl"), **kw), engine)
    sim = _SimToken("rocket")
    engine.handle(sim.create())
    return clock, engine, paper, sim


def alert_for(engine, sim, clock):
    t = engine.tokens[sim.mint]
    return {"ts": clock.t, "mint": sim.mint, "symbol": t.symbol, "name": t.name, "score": 85.0,
            "mcap_sol": t.mcap_sol, "change_5m": 1.0, "buyers_5m": 30, "flags": []}


def test_net_return_includes_all_costs(tmp_path):
    _, _, paper, _ = setup(tmp_path)
    ret, pnl = paper.net_return(100, 100)  # flat price still loses to fees, slippage and tx costs
    assert -0.18 < ret < -0.15 and pnl < 0
    ret, _ = paper.net_return(100, 200)
    assert 0.65 < ret < 0.75  # a 2x nets only ~+72% after costs
    assert paper.net_return(100, 0)[0] == -1.0  # can't lose more than the stake
    assert paper.net_return(100, 200, v_sol=40)[0] < paper.net_return(100, 200)[0]  # own sell impact


def test_feed_outage_pauses_fills(tmp_path):
    clock, engine, paper, sim = setup(tmp_path)
    paper.on_alert(alert_for(engine, sim, clock))
    engine.feed_status = "reconnecting"
    clock.t += 30
    paper.tick(clock.t)
    assert paper.open[sim.mint].entry_ts is None


def test_graduation_closes_with_haircut(tmp_path):
    clock, engine, paper, sim = setup(tmp_path, take_profit=100)
    paper.on_alert(alert_for(engine, sim, clock))
    clock.t += 6
    paper.tick(clock.t)
    engine.handle({"txType": "migrate", "mint": sim.mint})
    clock.t += 1
    paper.tick(clock.t)
    rec = paper.closed[-1]
    assert rec["exit_reason"] == "graduated" and rec["graduated"]
    assert rec["exit_mcap"] < engine.tokens[sim.mint].mcap_sol
    assert len(rec["marks"]) == 60 and set(rec["marks"].values()) == {round(rec["ret"], 4)}


def test_entry_is_delayed_and_minutes_are_marked(tmp_path):
    clock, engine, paper, sim = setup(tmp_path, entry_delay_s=2, take_profit=100, stop_loss=0.99)
    paper.on_alert(alert_for(engine, sim, clock))
    assert sim.mint in engine.pinned
    clock.t += 1
    engine.handle(sim.buy("late", 2.0))  # price spikes before our buy lands...
    spike = engine.tokens[sim.mint].mcap_sol
    engine.handle(sim.sell("late", 1.0))  # ...and comes back
    paper.tick(clock.t)
    assert paper.open[sim.mint].entry_ts is None
    clock.t += 1
    paper.tick(clock.t)
    p = paper.open[sim.mint]
    assert p.entry_mcap > spike  # filled at the worst price in the delay window
    for minute in range(1, 61):
        clock.t += 60
        engine.handle(sim.buy(f"w{minute}", 0.2))
        paper.tick(clock.t)
    clock.t += 2  # the max-hold sell lands
    paper.tick(clock.t)
    assert sim.mint not in paper.open and sim.mint not in engine.pinned
    rec = paper.closed[-1]
    assert rec["exit_reason"] == "max_hold"
    assert sorted(int(k) for k in rec["marks"]) == list(range(60, 3601, 60))
    assert rec["marks"]["60"] < rec["marks"]["3600"]  # steady buying -> later minutes higher


@pytest.mark.parametrize("move, reason", [("pump", "take_profit"), ("dump", "stop_loss"), ("dev", "dev_sold")])
def test_exit_rules(tmp_path, move, reason):
    clock, engine, paper, sim = setup(tmp_path)
    for i in range(20):
        engine.handle(sim.buy(f"w{i}", 0.5))
    paper.on_alert(alert_for(engine, sim, clock))
    clock.t += 6
    paper.tick(clock.t)
    clock.t += 1
    if move == "pump":
        engine.handle(sim.buy("big", 100))
    elif move == "dump":
        for i in range(20):
            engine.handle(sim.sell(f"w{i}", 1.0))
    else:
        engine.handle(sim.sell(sim.creator, 0.1))
    paper.tick(clock.t)
    p = paper.open[sim.mint]
    assert p.pending_exit and p.pending_exit[0] == reason and p.exit_ts is None
    clock.t += 2
    paper.tick(clock.t)
    assert p.exit_reason == reason
    assert (p.ret > 0) == (reason == "take_profit")


def test_results_persist_and_reload(tmp_path):
    clock, engine, paper, sim = setup(tmp_path, max_hold_s=60, horizons=(60,))
    paper.on_alert(alert_for(engine, sim, clock))
    for _ in range(70):
        clock.t += 1
        paper.tick(clock.t)
    assert len(paper.closed) == 1
    lines = (tmp_path / "trades.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["mint"] == sim.mint
    again = PaperTrader(paper.config, engine)
    assert len(again.closed) == 1
    s = summarize(again.closed, (60,))
    assert s["trades"] == 1 and s["best_minute"]["minute"] == 1
