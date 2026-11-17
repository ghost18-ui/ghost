"""Background scanner: pulls closed bars, runs the engine, stores and pushes signals."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback

import pandas as pd
import requests

import engine
from data import make_provider


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS signals(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, symbol TEXT, bar_time TEXT,
                side TEXT, payload TEXT, UNIQUE(symbol, bar_time, side))""")

    def _conn(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, symbol, sig) -> int | None:
        with self.lock, self._conn() as c:
            try:
                cur = c.execute("INSERT INTO signals(created,symbol,bar_time,side,payload) VALUES(?,?,?,?,?)",
                                (pd.Timestamp.now(tz="UTC").isoformat(), symbol, sig["time"], sig["side"],
                                 json.dumps({**sig, "symbol": symbol})))
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def max_id(self) -> int:
        with self.lock, self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id),0) FROM signals").fetchone()[0]

    def after_asc(self, after: int, limit=20):
        """Signals with id > after, oldest first, with age in seconds."""
        with self.lock, self._conn() as c:
            rows = c.execute("SELECT id, created, payload FROM signals WHERE id>? ORDER BY id ASC LIMIT ?",
                             (after, limit)).fetchall()
        now = pd.Timestamp.now(tz="UTC")
        out = []
        for i, created, payload in rows:
            d = json.loads(payload)
            d["id"] = i
            d["age"] = max(0, int((now - pd.Timestamp(created)).total_seconds()))
            out.append(d)
        return out

    def list(self, after=0, limit=50):
        with self.lock, self._conn() as c:
            rows = c.execute("SELECT id, created, payload FROM signals WHERE id>? ORDER BY id DESC LIMIT ?",
                             (after, limit)).fetchall()
        out = []
        for i, created, payload in rows:
            d = json.loads(payload)
            d["id"], d["created"] = i, created
            out.append(d)
        return out


def fmt_price(x):
    return f"{x:.5f}" if abs(x) < 20 else f"{x:.2f}"


def telegram(text: str):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=10)
    except Exception as e:  # never let a notification failure kill the scanner
        print("telegram failed:", e)


def format_signal(sym, s, interval):
    buy = s["side"] == "long"
    lines = [f"{'BUY' if buy else 'SELL'} {sym} ({interval}m) - grade {s['grade']}",
             f"Setup: {s['setup']} [{s['session']}]",
             f"Entry {fmt_price(s['entry'])}",
             f"SL    {fmt_price(s['sl'])}",
             f"TP1   {fmt_price(s['tp1'])} ({s['r_tp1']:.1f}R)",
             f"TP2   {fmt_price(s['tp2'])} ({s['r_tp2']:.1f}R)" + (" - NEARER than TP1" if s["tp2_inverted"] else ""),
             f"Bar opened {s['time'][:16].replace('T', ' ')} UTC (signal fires at its close)"]
    return "\n".join(lines)


class Scanner:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg
        self.store = store
        self.provider = make_provider(cfg.get("provider", "demo"))
        self.interval = int(cfg.get("interval_minutes", 1))
        self.history = max(int(cfg.get("history_bars", 1500)), engine.MIN_BARS + 100)
        self.base_params = cfg.get("params", {})
        self.state: dict[str, dict] = {}
        self.last_seen: dict[str, pd.Timestamp] = {}
        self.lock = threading.Lock()
        self.last_scan = None
        self.ea = {}   # last EA heartbeat: {"at": iso, "account": str, "mode": str, "dry": bool}

    # ------------------------------------------------------------------
    def scan_symbol(self, sym: dict):
        name = sym["name"]
        params = engine.Params.from_dict({**self.base_params, "mintick": sym.get("mintick", 0.00001)})
        df = self.provider.bars(sym, self.interval, self.history)
        frame, sigs = engine.run(df, params)
        last = frame.iloc[-1]
        vol_ok = bool(df["volume"].tail(200).sum() > 0)
        regime = "chop (no trades)" if last["chop"] else "trending"
        session = "London" if last["in_london"] else ("New York" if last["in_ny"] else "outside window")
        with self.lock:
            self.state[name] = {
                "name": name, "price": float(last["close"]), "bar_time": frame.index[-1].isoformat(),
                "session": session, "regime": regime, "adx": None if pd.isna(last["adx"]) else round(float(last["adx"]), 1),
                "rvol": None if pd.isna(last["rvol"]) else round(float(last["rvol"]), 2),
                "volume_ok": vol_ok, "status": "ok", "error": None,
                "bars": len(df),
            }
        new = []
        seen = self.last_seen.get(name)
        if seen is not None:
            for s in sigs:
                if pd.Timestamp(s["time"]) > seen:
                    new.append(s)
        self.last_seen[name] = frame.index[-1]
        for s in new:
            sid = self.store.add(name, s)
            if sid:
                print(f"SIGNAL {name} {s['side']} @ {s['entry']}")
                telegram(format_signal(name, s, self.interval))

    def scan_once(self):
        for sym in self.cfg["symbols"]:
            try:
                self.scan_symbol(sym)
            except Exception as e:
                traceback.print_exc()
                with self.lock:
                    st = self.state.get(sym["name"], {"name": sym["name"]})
                    st.update({"status": "error", "error": str(e)[:200]})
                    self.state[sym["name"]] = st
        self.last_scan = pd.Timestamp.now(tz="UTC").isoformat()

    def run_forever(self):
        step = self.interval * 60
        while True:
            self.scan_once()
            # wake a few seconds after the next bar closes
            time.sleep(step - (time.time() % step) + 5)

    def start(self):
        t = threading.Thread(target=self.run_forever, daemon=True, name="scanner")
        t.start()
        return t

    def snapshot(self):
        with self.lock:
            ea = dict(self.ea)
            if ea:
                ea["seconds_ago"] = int((pd.Timestamp.now(tz="UTC") - pd.Timestamp(ea["at"])).total_seconds())
            return {"provider": self.provider.name, "demo": self.provider.name == "demo",
                    "interval_minutes": self.interval, "last_scan": self.last_scan,
                    "symbols": list(self.state.values()), "ea": ea}
