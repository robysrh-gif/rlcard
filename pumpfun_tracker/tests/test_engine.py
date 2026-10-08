import random

from tracker.config import Config
from tracker.engine import Engine
from tracker.feeds import SimulatedFeed, _SimToken


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make_engine(**kw):
    clock = Clock()
    return Engine(Config(**kw), clock=clock), clock


def launch(engine, sim):
    assert engine.handle(sim.create()) == sim.mint
    return engine.tokens[sim.mint]


def test_create_registers_token_and_dev_buy():
    engine, _ = make_engine()
    sim = _SimToken("dud")
    token = launch(engine, sim)
    assert token.symbol == sim.symbol
    assert token.creator == sim.creator
    assert token.trade_count == 1
    assert token.mcap_sol > token.initial_mcap_sol
    assert engine.handle(sim.create()) is None  # duplicate create ignored


def test_organic_pump_scores_high_and_alerts():
    engine, clock = make_engine()
    sim = _SimToken("rocket")
    token = launch(engine, sim)
    wallets = [f"w{i}" for i in range(60)]
    for i in range(240):  # 4 minutes of steady, broad buying
        clock.t += 1
        engine.handle(sim.buy(wallets[i % 60], 0.4))
        if i % 6 == 0:
            engine.handle(sim.sell(wallets[(i + 7) % 60], 0.25) or {})
    alerts, _ = engine.tick()
    assert token.score >= 70, token.signals
    assert token.signals["change_5m"] > 1.0
    assert alerts == []  # conditions must hold for alert_sustain_s first
    clock.t += engine.config.alert_sustain_s
    alerts, _ = engine.tick()
    assert [a["mint"] for a in alerts] == [sim.mint]
    # Cooldown: no repeat alert on the next tick.
    clock.t += 1
    assert engine.tick()[0] == []


def test_brief_spike_does_not_alert():
    engine, clock = make_engine(alert_sustain_s=30)
    sim = _SimToken("rug")
    launch(engine, sim)
    for i in range(60):
        clock.t += 0.5
        engine.handle(sim.buy(f"w{i}", 0.6))
        engine.tick()
    engine.handle(sim.sell(sim.creator, 1.0))
    for _ in range(40):
        clock.t += 1
        assert engine.tick()[0] == []


def test_single_whale_is_penalised():
    engine, clock = make_engine()
    sim = _SimToken("whale")
    token = launch(engine, sim)
    for _ in range(30):
        clock.t += 2
        engine.handle(sim.buy("whale", 2.0))
    engine.tick()
    assert "whale_driven" in token.flags and "thin" in token.flags
    assert token.score < 40


def test_dev_dump_flags_and_penalises():
    engine, clock = make_engine()
    sim = _SimToken("rug")
    token = launch(engine, sim)
    for i in range(60):
        clock.t += 1
        engine.handle(sim.buy(f"w{i}", 0.5))
    engine.tick()
    before = token.score
    clock.t += 1
    engine.handle(sim.sell(sim.creator, 1.0))
    engine.tick()
    assert token.creator_sold and "dev_sold" in token.flags
    assert token.score < before * 0.7


def test_progress_and_migration():
    engine, clock = make_engine()
    sim = _SimToken("rocket")
    token = launch(engine, sim)
    while not sim.migrated:
        clock.t += 1
        for msg in [sim.buy(f"w{clock.t}", 5.0)]:
            engine.handle(msg)
        if sim.v_tokens <= 1_073_000_000 - 793_100_000:
            sim.migrated = True
    assert token.progress == 1.0
    engine.handle({"txType": "migrate", "mint": sim.mint})
    engine.tick()
    assert "graduated" in token.flags


def test_idle_tokens_are_pruned_and_cap_enforced():
    engine, clock = make_engine(max_tokens=5, idle_prune_s=60)
    mints = [launch(engine, _SimToken("dud")).mint for _ in range(8)]
    _, pruned = engine.tick()
    assert len(engine.tokens) == 5 and len(pruned) == 3
    clock.t += 61
    _, pruned = engine.tick()
    assert engine.tokens == {} and set(pruned) <= set(mints)


def test_simulated_feed_end_to_end():
    random.seed(7)
    engine, clock = make_engine()
    feed = SimulatedFeed(engine, launches_per_s=2.0)
    for _ in range(4 * 300):  # five simulated minutes
        clock.t += feed.dt
        feed.step()
        if int(clock.t * 4) % 4 == 0:
            engine.tick()
    assert engine.created_total > 400
    assert engine.alerts, "expected at least one rocket alert in five minutes of simulated flow"
    top = engine.ranked(5)
    assert top[0]["score"] >= top[-1]["score"]


def test_malformed_messages_are_dropped_without_side_effects():
    engine, _ = make_engine()
    sim = _SimToken("dud")
    token = launch(engine, sim)
    before = (token.mcap_sol, token.trade_count)
    bad = [
        [1, 2], "x", None,
        {"txType": "buy", "mint": ["x"]},
        {"txType": "buy", "mint": sim.mint, "solAmount": "abc", "marketCapSol": 999},
        {"txType": "buy", "mint": sim.mint, "solAmount": float("nan"), "marketCapSol": 999},
        {"txType": "sell", "mint": sim.mint, "solAmount": 1, "marketCapSol": float("inf")},
        {"txType": "create", "mint": "m2", "solAmount": {"a": 1}},
    ]
    for msg in bad:
        assert engine.handle(msg) is None
    assert (token.mcap_sol, token.trade_count) == before
    assert set(engine.tokens) == {sim.mint}


def test_text_fields_are_sanitised():
    engine, _ = make_engine()
    engine.handle({"txType": "create", "mint": "m1", "name": None, "symbol": 5,
                   "traderPublicKey": None, "uri": "javascript:alert(1)", "solAmount": 0})
    t = engine.tokens["m1"]
    assert (t.name, t.symbol, t.creator, t.uri) == ("", "", "", "")
    engine.handle({"txType": "create", "mint": "m2", "uri": "https://ipfs.io/x", "solAmount": 0})
    assert engine.tokens["m2"].uri == "https://ipfs.io/x"
