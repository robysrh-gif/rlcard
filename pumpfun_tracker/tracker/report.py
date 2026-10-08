"""Print a paper-trading report:  python -m tracker.report [paper_trades.jsonl] [--watch]

--watch reprints the report every minute.
"""
import os
import sys

from .paper import load_records, summarize


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def main(path: str = "paper_trades.jsonl") -> None:
    if not os.path.exists(path):
        print(f"No paper trades yet ({path} not found). Run the tracker and wait for alerts.")
        return
    records = load_records(path)
    if "simulated" in os.path.basename(path):
        print("*** SIMULATED DATA: this checks that the plumbing works and says NOTHING about real profits. ***\n")
    s = summarize(records)
    if not s["trades"]:
        print("No finished paper trades yet.")
        return
    staked = s["staked"]
    print(f"Paper trades: {s['trades']}   (stake per trade ~{staked / s['trades']:.3f} SOL)")
    print(f"Win rate:     {s['win_rate'] * 100:.0f}%")
    print(f"Avg return:   {pct(s['avg_ret'])}   median {pct(s['median_ret'])}")
    print(f"Best / worst: {pct(s['best'])} / {pct(s['worst'])}")
    print(f"Total P&L:    {s['pnl_sol']:+.4f} SOL   (ROI {pct(s['roi'])} on {staked:.3f} SOL staked)")
    print("\nExit reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(s["exits"].items())))
    print("\n--- Hindsight (you can't know these in advance; don't tune on them alone) ---")
    print(f"Avg peak after entry: {pct(s['avg_peak_gain'])}  (if you had sold at the exact top, before costs)")
    print("\nIf you had simply held and sold at minute N (no exit rules):")
    print("  min   avg return  win rate     n")
    best = s.get("best_minute", {}).get("minute")
    for h, v in s["hold"].items():
        m = int(h) // 60
        bar = "#" * min(int(abs(v["avg_ret"]) * 20), 30)
        mark = "  <- best" if m == best else ""
        print(f"  {m:>3}   {pct(v['avg_ret']):>9}   {v['win_rate'] * 100:>6.0f}%  {v['n']:>4}  "
              f"{'+' if v['avg_ret'] >= 0 else '-'}{bar}{mark}")
    if s["graduated"]:
        print(f"\n{s['graduated']} trades graduated off pump.fun, where the feed loses the price. Minute marks stop "
              "there, so later minutes average fewer trades (see n).")
    print("\nBy alert score:")
    for b in s["by_score"]:
        print(f"  {b['range']:>7}: n={b['n']:<4} win rate {b['win_rate'] * 100:.0f}%  avg {pct(b['avg_ret'])}")
    if s["trades"] < 50:
        print(f"\nOnly {s['trades']} trades so far. Wait for at least 50-100 before trusting these numbers.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--watch"]
    if "--watch" in sys.argv:
        import time
        while True:  # refresh every minute; Ctrl+C to stop
            print("\033[2J\033[H" + time.strftime("%H:%M:%S"))
            main(*args[:1])
            time.sleep(60)
    else:
        main(*args[:1])
