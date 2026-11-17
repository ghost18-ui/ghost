"""Backtest the engine's signals on historical bars.

Examples
  python backtest.py --provider demo --symbol EUR/USD --bars 20000
  python backtest.py --csv eurusd_1m.csv --symbol EUR/USD --partial 0.5

Trade model (documented so you can judge it):
  * Entry at the signal bar's close.
  * Stage 0: stop = SL, target = TP1. If a bar touches both SL and TP1 the
    STOP is assumed first (conservative).
  * At TP1, `partial` of the position is closed at TP1 and the stop moves to
    break-even for the rest. The remainder exits at TP2, at break-even, or at
    the close after `max_hold` bars.
  * If TP2 is nearer than TP1 (see engine docs) TP2 is pushed out to TP1 for
    the simulation, and the count is reported.
  * No spread, slippage or commission unless you pass --cost-r.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

import engine
from data import make_provider, Csv


def simulate(df: pd.DataFrame, sigs: list[dict], partial=0.5, max_hold=240, cost_r=0.0):
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    n = len(df)
    trades = []
    for s in sigs:
        i = s["bar_pos"]
        long = s["side"] == "long"
        sg = 1.0 if long else -1.0
        entry, sl, tp1, tp2, risk = s["entry"], s["sl"], s["tp1"], s["tp2"], s["risk"]
        adjusted = s["tp2_inverted"]
        if adjusted:
            tp2 = tp1
        stage, exit_r, reason, exit_bar = 0, None, None, None
        part_r = partial * (abs(tp1 - entry) / risk)
        stop = sl
        for j in range(i + 1, min(n, i + 1 + max_hold)):
            hit_stop = (l[j] <= stop) if long else (h[j] >= stop)
            hit_tp1 = (h[j] >= tp1) if long else (l[j] <= tp1)
            hit_tp2 = (h[j] >= tp2) if long else (l[j] <= tp2)
            if stage == 0:
                if hit_stop:
                    exit_r, reason, exit_bar = -1.0, "SL", j
                    break
                if hit_tp1:
                    stage, stop = 1, entry
                    if hit_tp2:
                        exit_r = part_r + (1 - partial) * (abs(tp2 - entry) / risk)
                        reason, exit_bar = "TP2", j
                        break
            else:
                if hit_stop:
                    exit_r, reason, exit_bar = part_r, "BE", j
                    break
                if hit_tp2:
                    exit_r = part_r + (1 - partial) * (abs(tp2 - entry) / risk)
                    reason, exit_bar = "TP2", j
                    break
        if exit_r is None:  # timeout or ran out of data
            j = min(n - 1, i + max_hold)
            open_r = sg * (c[j] - entry) / risk
            exit_r = open_r if stage == 0 else part_r + (1 - partial) * open_r
            reason, exit_bar = "TIMEOUT", j
        trades.append({**{k: s[k] for k in ("time", "side", "setup", "grade", "session")},
                       "entry": entry, "sl": sl, "tp1": s["tp1"], "tp2": s["tp2"],
                       "tp2_adjusted": adjusted, "exit": reason, "bars_held": int(exit_bar - i),
                       "r": float(exit_r - cost_r)})
    return pd.DataFrame(trades)


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    r = t["r"]
    wins, losses = r[r > 0], r[r <= 0]
    cum = r.cumsum()
    dd = (cum.cummax() - cum).max()
    return {
        "trades": int(len(t)),
        "win_rate_pct": round(100 * len(wins) / len(t), 1),
        "avg_r": round(float(r.mean()), 3),
        "total_r": round(float(r.sum()), 2),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
        "max_drawdown_r": round(float(dd), 2),
        "exits": t["exit"].value_counts().to_dict(),
        "tp2_adjusted": int(t["tp2_adjusted"].sum()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--provider", help="demo | twelvedata | oanda (default: from config)")
    ap.add_argument("--csv", help="CSV file of bars instead of a provider")
    ap.add_argument("--symbol", help="symbol name from config (default: first)")
    ap.add_argument("--interval", type=int)
    ap.add_argument("--bars", type=int, default=20000)
    ap.add_argument("--partial", type=float, default=0.5)
    ap.add_argument("--max-hold", type=int, default=240)
    ap.add_argument("--cost-r", type=float, default=0.0)
    ap.add_argument("--out", default="backtest_trades.csv")
    a = ap.parse_args()

    cfg = json.load(open(a.config))
    interval = a.interval or cfg.get("interval_minutes", 1)
    syms = {s["name"]: s for s in cfg["symbols"]}
    sym = syms.get(a.symbol) if a.symbol else cfg["symbols"][0]
    if sym is None:
        sym = {"name": a.symbol}
    prov = Csv(a.csv) if a.csv else make_provider(a.provider or cfg.get("provider", "demo"))
    df = prov.bars(sym, interval, a.bars)
    params = engine.Params.from_dict({**cfg.get("params", {}), "mintick": sym.get("mintick", 0.00001)})
    frame, sigs = engine.run(df, params)

    print(f"{sym['name']}  {interval}m  {len(df)} bars  {df.index[0]} -> {df.index[-1]}  [{prov.name}]")
    if df["volume"].sum() == 0:
        print("WARNING: no volume in this data - RVOL/CVD/profile rules cannot work.")
    if prov.name == "demo":
        print("NOTE: demo data is synthetic. These numbers say nothing about real markets.")
    t = simulate(df, sigs, a.partial, a.max_hold, a.cost_r)
    s = stats(t)
    print(json.dumps(s, indent=2))
    if not t.empty:
        t.to_csv(a.out, index=False)
        print(f"trades written to {a.out}")
        print("\nBy setup:")
        print(t.groupby("setup")["r"].agg(["count", "mean", "sum"]).round(3).to_string())
    days = max((df.index[-1] - df.index[0]).days, 1)
    if s["trades"] < 30:
        print(f"\nOnly {s['trades']} trades over {days} days: too few to judge win rate. "
              "Use a longer history (tens of trades at minimum, ideally 100+).")


if __name__ == "__main__":
    sys.exit(main())
