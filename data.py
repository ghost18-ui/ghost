"""Market-data providers. Every provider returns CLOSED bars only, as an
OHLCV DataFrame with a UTC DatetimeIndex (oldest first)."""
from __future__ import annotations

import os
import zlib

import numpy as np
import pandas as pd
import requests

COLS = ["open", "high", "low", "close", "volume"]


def drop_forming(df: pd.DataFrame, interval_min: int) -> pd.DataFrame:
    """Remove the last bar if it has not finished yet."""
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="UTC")
    while len(df) and df.index[-1] + pd.Timedelta(minutes=interval_min) > now:
        df = df.iloc[:-1]
    return df


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------------------
class TwelveData:
    """https://twelvedata.com  (env TWELVEDATA_KEY). Forex/metals/energy/stocks.
    NOTE: spot forex and many CFD-like symbols return NO volume here, which the
    strategy needs (RVOL, CVD, profiles). Check the dashboard's volume warning."""
    name = "twelvedata"
    MAP = {1: "1min", 5: "5min", 15: "15min", 30: "30min", 60: "1h"}

    def __init__(self):
        self.key = os.environ.get("TWELVEDATA_KEY", "")
        if not self.key:
            raise ProviderError("TWELVEDATA_KEY env var is not set")

    def bars(self, sym: dict, interval_min: int, count: int) -> pd.DataFrame:
        symbol = sym.get("twelvedata") or sym["name"]
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol, "interval": self.MAP[interval_min],
            "outputsize": min(count, 5000), "timezone": "UTC",
            "order": "ASC", "apikey": self.key}, timeout=20)
        j = r.json()
        if j.get("status") == "error" or "values" not in j:
            raise ProviderError(f"twelvedata: {j.get('message', r.text[:120])}")
        df = pd.DataFrame(j["values"])
        df.index = pd.to_datetime(df["datetime"], utc=True)
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = df[COLS].astype(float).sort_index()
        return drop_forming(df, interval_min)


class Oanda:
    """OANDA v20 REST (env OANDA_TOKEN, OANDA_ENV=practice|live). Gives tick
    volume for forex, metals, energy and index CFDs. Availability of CFDs
    depends on your OANDA entity/region."""
    name = "oanda"
    GRAN = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1"}

    def __init__(self):
        self.token = os.environ.get("OANDA_TOKEN", "")
        if not self.token:
            raise ProviderError("OANDA_TOKEN env var is not set")
        env = os.environ.get("OANDA_ENV", "practice")
        self.base = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"

    def _page(self, inst, gran, count, to=None):
        params = {"granularity": gran, "count": min(count, 5000), "price": "M"}
        if to is not None:
            params["to"] = to
        r = requests.get(f"{self.base}/v3/instruments/{inst}/candles", params=params,
                         headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
        j = r.json()
        if "candles" not in j:
            raise ProviderError(f"oanda: {j.get('errorMessage', r.text[:120])}")
        rows = [(pd.to_datetime(x["time"], utc=True), float(x["mid"]["o"]), float(x["mid"]["h"]),
                 float(x["mid"]["l"]), float(x["mid"]["c"]), float(x["volume"]))
                for x in j["candles"] if x.get("complete")]
        return pd.DataFrame(rows, columns=["t"] + COLS).set_index("t")

    def bars(self, sym: dict, interval_min: int, count: int) -> pd.DataFrame:
        inst = sym.get("oanda") or sym["name"].replace("/", "_")
        gran = self.GRAN[interval_min]
        pages, remaining, to = [], count, None
        while remaining > 0:
            requested = min(remaining, 5000)
            page = self._page(inst, gran, requested, to)
            if page.empty:
                break
            pages.append(page)
            remaining -= len(page)
            to = page.index[0].isoformat()
            if len(page) < requested:
                break
        if not pages:
            raise ProviderError("oanda returned no candles")
        df = pd.concat(pages[::-1])
        return df[~df.index.duplicated()].sort_index()


class Demo:
    """Synthetic random-walk data so the whole pipeline runs without any API
    key. Signals produced from it are MEANINGLESS."""
    name = "demo"
    BASE = {"EUR/USD": 1.08, "GBP/USD": 1.27, "XAU/USD": 2650.0, "WTI": 70.0, "US100": 20500.0, "US30": 43500.0}

    def bars(self, sym: dict, interval_min: int, count: int) -> pd.DataFrame:
        name = sym["name"]
        last_open = pd.Timestamp.now(tz="UTC").floor(f"{interval_min}min") - pd.Timedelta(minutes=interval_min)
        idx_all = pd.date_range(end=last_open, periods=count, freq=f"{interval_min}min")
        anchor = idx_all[0].floor("D")
        idx = pd.date_range(anchor, last_open, freq=f"{interval_min}min")
        rng = np.random.default_rng(zlib.crc32(name.encode()) ^ (anchor.value & 0xFFFFFFFF))
        m = len(idx)
        base = self.BASE.get(name, 100.0)
        ny = idx.tz_convert("America/New_York")
        hr = np.asarray(ny.hour + ny.minute / 60.0)
        active = ((hr >= 3) & (hr < 5)) | ((hr >= 9.5) & (hr < 11.5))
        sigma = base * 0.00015 * np.where(active, 2.2, 1.0)
        ret = rng.normal(0, 1, m) * sigma
        # slow oscillating drift so the series trends and ranges
        ret += base * 0.00002 * np.sin(np.arange(m) / 240.0)
        close = base + np.cumsum(ret)
        open_ = np.r_[base, close[:-1]]
        wick_u = rng.exponential(1, m) * sigma * 0.6
        wick_d = rng.exponential(1, m) * sigma * 0.6
        high = np.maximum(open_, close) + wick_u
        low = np.minimum(open_, close) - wick_d
        vol = rng.lognormal(6.0, 0.5, m) * np.where(active, 2.5, 1.0) * (1 + 3 * np.abs(ret) / sigma)
        df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
        return df.iloc[-count:]


class Csv:
    """Load bars from a CSV (columns: time/datetime, open, high, low, close[, volume])."""
    name = "csv"

    def __init__(self, path: str):
        self.path = path

    def bars(self, sym: dict, interval_min: int, count: int) -> pd.DataFrame:
        df = pd.read_csv(self.path)
        df.columns = [c.lower() for c in df.columns]
        tcol = next((c for c in ("time", "datetime", "date", "timestamp") if c in df.columns), None)
        if tcol is None:
            raise ProviderError("CSV needs a time/datetime column")
        t = df[tcol]
        if pd.api.types.is_numeric_dtype(t):
            df.index = pd.to_datetime(t, unit="s", utc=True)
        else:
            df.index = pd.to_datetime(t, utc=True)
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = df[COLS].astype(float).sort_index()
        return df.iloc[-count:] if count else df



class Yahoo:
    """Free Yahoo Finance server-side OHLCV provider (no API key required)."""
    name = "yahoo"
    MAP = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h"}

    def bars(self, sym: dict, interval_min: int, count: int) -> pd.DataFrame:
        symbol = sym.get("yahoo") or sym.get("twelvedata") or sym["name"]
        interval = self.MAP.get(interval_min, "5m")
        rng = "5d" if interval_min <= 15 else "1mo"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = requests.get(url, params={"interval": interval, "range": rng},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        j = r.json()
        res = (j.get("chart") or {}).get("result") or []
        if not res:
            err = ((j.get("chart") or {}).get("error") or {}).get("description", r.text[:120])
            raise ProviderError(f"yahoo: {err}")
        r0 = res[0]
        ts = r0.get("timestamp") or []
        q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
        rows = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            v = q["volume"][i] if q.get("volume") else 0.0
            if None not in (o, h, l, c):
                rows.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": float(v or 0.0)})
        if not rows:
            raise ProviderError(f"yahoo: no bars for {symbol}")
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df[COLS].astype(float).sort_index()
        df = drop_forming(df, interval_min)
        return df.iloc[-count:] if count else df


def make_provider(name: str):
    name = (name or "demo").lower()
    if name == "yahoo":
        return Yahoo()
    if name == "twelvedata":
        return TwelveData()
    if name == "oanda":
        return Oanda()
    if name == "demo":
        return Demo()
    raise ProviderError(f"unknown provider '{name}'")
