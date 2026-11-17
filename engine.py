"""
Python port of the TradingView script
"Fabio + Carmine + Marco + Andrea Master Engine (Indicator v8)".

Same rules, same defaults. Only closed bars are evaluated (the Pine script
uses barstate.isconfirmed, so alerts fire on bar close).

Known behaviours carried over from the Pine source (deliberately NOT changed):
  * inReboundMode is declared but never set to true, so "Monster Rebound"
    (3.0R targets) never activates. Param `in_rebound_mode` exists if you
    want to force it on.
  * Ultra mode makes the A+ grade condition always true, so every signal is
    graded A+. The grade is cosmetic, not a probability.
  * TP2 is the prior-day reference POC when it lies beyond price, which can be
    NEARER than TP1. Such signals are flagged with tp2_inverted = True.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import numpy as np
import pandas as pd


@dataclass
class Params:
    use_ultra: bool = True
    in_rebound_mode: bool = False          # never true in the Pine script
    enable_london: bool = True
    london_sess: str = "0300-0430"          # America/New_York
    enable_ny: bool = True
    ny_sess: str = "0930-1100"              # America/New_York
    tz: str = "America/New_York"
    use_chop: bool = True
    adx_threshold: int = 18
    use_htf: bool = True
    htf_len: int = 50
    ref_len: int = 390
    ref_off: int = 30
    leg_len: int = 60
    bins: int = 24
    va_pct: float = 70.0
    lvn_mult: float = 0.30
    swing_look: int = 15
    use_marco: bool = True
    use_cvd_div: bool = True
    rvol_base: float = 1.5
    rvol_rebound: float = 1.8
    use_absorb: bool = True
    wick_base: float = 0.30
    wick_rebound: float = 0.35
    delta_len: int = 3
    rr1: float = 0.8
    rr2_std: float = 2.0
    rr2_reb: float = 3.0
    atr_len: int = 14
    tol_atr: float = 0.25
    stop_buf: float = 0.30
    max_risk: float = 2.5
    cooldown: int = 5
    mintick: float = 0.00001

    @classmethod
    def from_dict(cls, d: dict | None):
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


MIN_BARS = 430  # ref_off + ref_len + a little warm-up


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Return a clean OHLCV frame with a sorted, tz-aware UTC DatetimeIndex."""
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if not isinstance(df.index, pd.DatetimeIndex):
        for c in ("time", "datetime", "date", "timestamp"):
            if c in df.columns:
                df.index = pd.to_datetime(df[c], utc=True)
                break
        else:
            raise ValueError("DataFrame needs a DatetimeIndex or a time column")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["open", "high", "low", "close"])


def _parse_session(spec: str):
    a, b = spec.replace(":", "").split("-")
    return int(a[:2]) * 60 + int(a[2:]), int(b[:2]) * 60 + int(b[2:])


def _in_session(minutes: np.ndarray, spec: str) -> np.ndarray:
    s, e = _parse_session(spec)
    if s < e:
        return (minutes >= s) & (minutes < e)
    return (minutes >= s) | (minutes < e)


def _rma(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1.0 / n, adjust=False).mean()


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan, dtype=float)
    if k < len(a):
        out[k:] = a[:-k]
    return out


def _dmi(h: pd.Series, l: pd.Series, c: pd.Series, length=14, adx_smooth=14):
    up = h.diff()
    down = -l.diff()
    plus = np.where((up > down) & (up > 0), up, 0.0)
    minus = np.where((down > up) & (down > 0), down, 0.0)
    plus = pd.Series(plus, index=h.index)
    minus = pd.Series(minus, index=h.index)
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    tr.iloc[0] = h.iloc[0] - l.iloc[0]
    trur = _rma(tr, length)
    plus_di = (100 * _rma(plus, length) / trur).ffill()
    minus_di = (100 * _rma(minus, length) / trur).ffill()
    s = plus_di + minus_di
    adx = 100 * _rma((plus_di - minus_di).abs() / s.replace(0, 1), adx_smooth)
    return plus_di, minus_di, adx, trur  # trur == ATR(length) when length == atr_len


def profile(high, low, hlc3, vol, t, off, length, nb, va_pct, lvn_mult, close_t, mintick):
    """Volume profile over bars [t-off-length+1 .. t-off].

    Returns (poc, vah, val, lvn_below_close, lvn_above_close); NaN when not
    enough history. Mirrors f_profile() in the Pine script.
    """
    nan = float("nan")
    if t < off + length:
        return nan, nan, nan, nan, nan
    s, e = t - off - length + 1, t - off + 1
    lo = float(low[s:e].min())
    hi = float(high[s:e].max())
    stp = max((hi - lo) / nb, mintick)
    k = np.floor((hlc3[s:e] - lo) / stp).astype(int)
    k = np.clip(k, 0, nb - 1)
    vols = np.bincount(k, weights=vol[s:e], minlength=nb).astype(float)
    total = vols.sum()
    poc_k = int(np.argmax(vols))
    acc = vols[poc_k]
    up_k = dn_k = poc_k
    while acc < total * va_pct / 100.0 and (up_k < nb - 1 or dn_k > 0):
        v_up = vols[up_k + 1] if up_k < nb - 1 else -1.0
        v_dn = vols[dn_k - 1] if dn_k > 0 else -1.0
        if v_up >= v_dn:
            up_k += 1
            acc += v_up
        else:
            dn_k -= 1
            acc += v_dn
    poc = lo + (poc_k + 0.5) * stp
    vah = lo + (up_k + 1) * stp
    val = lo + dn_k * stp
    avg_row = total / nb
    lvn_lo = lvn_hi = nan
    for kk in range(1, nb - 1):
        v = vols[kk]
        if v < avg_row * lvn_mult and v <= vols[kk - 1] and v <= vols[kk + 1]:
            px = lo + (kk + 0.5) * stp
            if px < close_t and (np.isnan(lvn_lo) or px > lvn_lo):
                lvn_lo = px
            if px > close_t and (np.isnan(lvn_hi) or px < lvn_hi):
                lvn_hi = px
    return poc, vah, val, lvn_lo, lvn_hi


# ----------------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------------
def run(df_in: pd.DataFrame, p: Params | None = None):
    """Evaluate the engine on closed bars.

    Returns (frame, signals). `frame` holds indicator columns per bar;
    `signals` is a list of dicts, oldest first.
    """
    p = p or Params()
    df = normalize(df_in)
    n = len(df)
    if n < MIN_BARS:
        raise ValueError(f"need at least {MIN_BARS} bars, got {n}")

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = np.nan_to_num(df["volume"].to_numpy())
    hlc3 = (h + l + c) / 3.0
    hs, ls, cs = df["high"], df["low"], df["close"]

    ny = df.index.tz_convert(p.tz)
    minutes = np.asarray(ny.hour * 60 + ny.minute)
    dates = np.asarray(ny.normalize().asi8)
    is_new_day = np.r_[False, dates[1:] != dates[:-1]]

    in_ldn = p.enable_london & _in_session(minutes, p.london_sess)
    in_ny = p.enable_ny & _in_session(minutes, p.ny_sess)
    ldn_start = in_ldn & ~np.r_[False, in_ldn[:-1]]

    with np.errstate(invalid="ignore", divide="ignore"):
        # ---- indicators ---------------------------------------------------
        _, _, adx_s, _ = _dmi(hs, ls, cs, 14, 14)
        adx = adx_s.to_numpy()
        eff_adx = max(p.adx_threshold, 20) if p.use_ultra else p.adx_threshold
        chop = (adx < eff_adx) if p.use_chop else np.zeros(n, bool)

        prev_c = cs.shift(1)
        tr = pd.concat([hs - ls, (hs - prev_c).abs(), (ls - prev_c).abs()], axis=1).max(axis=1)
        tr.iloc[0] = hs.iloc[0] - ls.iloc[0]
        atr = _rma(tr, p.atr_len).to_numpy()
        tol = atr * p.tol_atr
        rng = np.maximum(h - l, p.mintick)

        htf_ema = cs.ewm(span=p.htf_len, adjust=False).mean().to_numpy()
        htf_bull = (c > htf_ema) if p.use_htf else np.ones(n, bool)
        htf_bear = (c < htf_ema) if p.use_htf else np.ones(n, bool)

        swing_h = _shift(hs.rolling(p.swing_look).max().to_numpy(), 1)
        swing_l = _shift(ls.rolling(p.swing_look).min().to_numpy(), 1)
        marco_bull = (l < swing_l) & (c > swing_l) if p.use_marco else np.ones(n, bool)
        marco_bear = (h > swing_h) & (c < swing_h) if p.use_marco else np.ones(n, bool)

        d_bar = v * ((c - l) - (h - c)) / rng
        d_sum = pd.Series(d_bar).rolling(p.delta_len).sum().to_numpy()
        cvd = np.cumsum(d_bar)
        bull_cvd = ((l < _shift(l, 5)) & (cvd > _shift(cvd, 5))) if p.use_cvd_div else np.ones(n, bool)
        bear_cvd = ((h > _shift(h, 5)) & (cvd < _shift(cvd, 5))) if p.use_cvd_div else np.ones(n, bool)

        vol_sma = pd.Series(v).rolling(20).mean().to_numpy()
        rvol = v / vol_sma
        boosted = p.in_rebound_mode or p.use_ultra
        act_rvol = p.rvol_rebound if boosted else p.rvol_base
        act_wick = p.wick_rebound if boosted else p.wick_base
        vol_ok = rvol >= act_rvol

        lower_wick = (np.minimum(o, c) - l) / rng
        upper_wick = (h - np.maximum(o, c)) / rng
        bull_absorb = (lower_wick >= act_wick) if p.use_absorb else np.ones(n, bool)
        bear_absorb = (upper_wick >= act_wick) if p.use_absorb else np.ones(n, bool)
        body_bull = ((c - l) / rng >= 0.60) if p.use_ultra else np.ones(n, bool)
        body_bear = ((h - c) / rng >= 0.60) if p.use_ultra else np.ones(n, bool)

        agg_bull = (d_sum > 0) & (d_bar > 0) & vol_ok & (c > o) & bull_absorb & bull_cvd & body_bull
        agg_bear = (d_sum < 0) & (d_bar < 0) & vol_ok & (c < o) & bear_absorb & bear_cvd & body_bear

    # ---- volume profiles (only where needed: sessions + new-day bars) ------
    need = in_ldn | in_ny | is_new_day
    ref_poc = np.full(n, np.nan)
    ref_vah = np.full(n, np.nan)
    ref_val = np.full(n, np.nan)
    lvn_b = np.full(n, np.nan)
    lvn_a = np.full(n, np.nan)
    for t in np.flatnonzero(need):
        poc, vah, val, _, _ = profile(h, l, hlc3, v, t, p.ref_off, p.ref_len, p.bins,
                                      p.va_pct, p.lvn_mult, c[t], p.mintick)
        ref_poc[t], ref_vah[t], ref_val[t] = poc, vah, val
        if in_ldn[t] or in_ny[t]:
            _, _, _, lb, la = profile(h, l, hlc3, v, t, 1, p.leg_len, p.bins,
                                      p.va_pct, p.lvn_mult, c[t], p.mintick)
            lvn_b[t], lvn_a[t] = lb, la

    # ---- stateful series: London range, prior reference POC -----------------
    ldn_h = np.full(n, np.nan)
    ldn_l = np.full(n, np.nan)
    n_poc = np.full(n, np.nan)
    cur_h = cur_l = cur_poc = np.nan
    for t in range(n):
        if ldn_start[t]:
            cur_h, cur_l = h[t], l[t]
        elif in_ldn[t]:
            cur_h, cur_l = max(cur_h, h[t]), min(cur_l, l[t])
        if is_new_day[t] and not np.isnan(ref_poc[t]):
            cur_poc = ref_poc[t]
        ldn_h[t], ldn_l[t], n_poc[t] = cur_h, cur_l, cur_poc

    with np.errstate(invalid="ignore"):
        fa_bull = (l < ref_val) & (c > ref_val) & (c < ref_poc)
        fa_bear = (h > ref_vah) & (c < ref_vah) & (c > ref_poc)
        has_lb, has_la = ~np.isnan(lvn_b), ~np.isnan(lvn_a)

        l1_long = in_ldn & marco_bull & fa_bull & agg_bull
        l1_short = in_ldn & marco_bear & fa_bear & agg_bear
        l2_long = in_ldn & ~chop & (c > ref_vah) & has_lb & (l <= lvn_b + tol) & (c > lvn_b) & agg_bull & htf_bull
        l2_short = in_ldn & ~chop & (c < ref_val) & has_la & (h >= lvn_a - tol) & (c < lvn_a) & agg_bear & htf_bear
        mb = marco_bull if p.use_ultra else np.ones(n, bool)
        ms = marco_bear if p.use_ultra else np.ones(n, bool)
        ny1_long = in_ny & ~np.isnan(ldn_h) & (c > ldn_h) & (l <= ldn_h + tol) & mb & agg_bull & htf_bull
        ny1_short = in_ny & ~np.isnan(ldn_l) & (c < ldn_l) & (h >= ldn_l - tol) & ms & agg_bear & htf_bear
        ny2_long = in_ny & (c > ref_poc) & has_lb & (l <= lvn_b + tol) & (c > lvn_b) & agg_bull & htf_bull
        ny2_short = in_ny & (c < ref_poc) & has_la & (h >= lvn_a - tol) & (c < lvn_a) & agg_bear & htf_bear

        long_any = l1_long | l2_long | ny1_long | ny2_long
        short_any = l1_short | l2_short | ny1_short | ny2_short

        long_lvl = np.where(has_lb, lvn_b, l)
        long_sl = np.minimum(l, long_lvl) - atr * p.stop_buf
        long_risk = c - long_sl
        valid_long = (long_risk > 0) & (long_risk <= atr * p.max_risk)
        short_lvl = np.where(has_la, lvn_a, h)
        short_sl = np.maximum(h, short_lvl) + atr * p.stop_buf
        short_risk = short_sl - c
        valid_short = (short_risk > 0) & (short_risk <= atr * p.max_risk)

    long_names = [("L1 failed-auction sweep", l1_long), ("L2 LVN pullback (London)", l2_long),
                  ("NY1 London-high retest", ny1_long), ("NY2 LVN pullback (NY)", ny2_long)]
    short_names = [("L1 failed-auction sweep", l1_short), ("L2 LVN pullback (London)", l2_short),
                   ("NY1 London-low retest", ny1_short), ("NY2 LVN pullback (NY)", ny2_short)]

    # ---- signal loop (cooldown is stateful) -------------------------------
    sigs = []
    long_flag = np.zeros(n, bool)
    short_flag = np.zeros(n, bool)
    last_bar = None
    for t in range(n):
        cd_ok = last_bar is None or (t - last_bar >= p.cooldown)
        if not ((in_ldn[t] or in_ny[t]) and not chop[t] and cd_ok):
            continue
        fired_this_bar = False
        for side in ("long", "short"):
            if side == "long":
                ok = long_any[t] and valid_long[t]
                risk, sl, names = long_risk[t], long_sl[t], long_names
                sgn = 1.0
            else:
                ok = short_any[t] and valid_short[t]
                risk, sl, names = short_risk[t], short_sl[t], short_names
                sgn = -1.0
            if not ok:
                continue
            fired_this_bar = True
            entry = c[t]
            tp1 = entry + sgn * risk * p.rr1
            np_t = n_poc[t]
            if p.in_rebound_mode:
                tp2 = entry + sgn * risk * p.rr2_reb
            elif chop[t]:
                tp2 = ref_poc[t] if not np.isnan(ref_poc[t]) else entry + sgn * risk * 1.5
            else:
                beyond = (not np.isnan(np_t)) and ((np_t > entry) if side == "long" else (np_t < entry))
                tp2 = np_t if beyond else entry + sgn * risk * p.rr2_std
            wick = lower_wick[t] if side == "long" else upper_wick[t]
            grade = "A+" if (wick >= 0.35 and rvol[t] >= 1.5) else "A"
            inverted = (tp2 < tp1) if side == "long" else (tp2 > tp1)
            (long_flag if side == "long" else short_flag)[t] = True
            sigs.append({
                "bar_pos": int(t),
                "time": df.index[t].isoformat(),
                "side": side,
                "setup": " + ".join(nm for nm, arr in names if arr[t]),
                "grade": grade,
                "entry": float(entry),
                "sl": float(sl),
                "tp1": float(tp1),
                "tp2": float(tp2),
                "entry2": float(entry - sgn * atr[t] * 0.15),
                "entry3": float(entry - sgn * atr[t] * 0.30),
                "risk": float(risk),
                "r_tp1": float(p.rr1),
                "r_tp2": float(abs(tp2 - entry) / risk),
                "tp2_inverted": bool(inverted),
                "atr": float(atr[t]),
                "rvol": float(rvol[t]),
                "session": "London" if in_ldn[t] else "New York",
            })
        if fired_this_bar:
            last_bar = t

    frame = pd.DataFrame({
        "close": c, "adx": adx, "chop": chop, "in_london": in_ldn, "in_ny": in_ny,
        "rvol": rvol, "atr": atr, "ref_poc": ref_poc, "ref_vah": ref_vah, "ref_val": ref_val,
        "lvn_below": lvn_b, "lvn_above": lvn_a, "ldn_high": ldn_h, "ldn_low": ldn_l,
        "long_signal": long_flag, "short_signal": short_flag,
    }, index=df.index)
    return frame, sigs


def params_dict(p: Params) -> dict:
    return asdict(p)
