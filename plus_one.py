#!/usr/bin/env python3
"""Turtle Soup Plus One — frozen Raschke & Connors (*Street Smarts*, ch. 5) rules.

Validated on liquid BingX USDT-M perps (24h quote volume >= $5M):
  4h primary, 1d secondary. Exit 2R or at the close of the 8th bar.
  Holdout 2026-02-12..2026-11-11, 31 coins: +0.181R/trade, PF 1.33, 25/31,
  4.24 fills/day, a setup on 91.6% of calendar days.
  Costs 0.07%/side, next-bar fill only if price trades through.
  Classic Turtle Soup (ch. 4, no close confirmation) is net-negative on 4h.
  Pure ICT sweep→MSS→FVG did not survive the same tests. Session/kill-zone
  filters were tried and failed DEV gates — they are NOT applied here.

Rules (frozen):
  LONG  if the last CLOSED bar makes a new 20-bar low, the previous 20-bar
        low is at least 3 bars earlier, AND that bar CLOSES at or below the
        prior low. Next bar: buy stop AT the prior 20-bar low. Cancel if
        unfilled. Stop = 0.02% under the lower of Day-1 / Day-2 lows.
        Target 2R, time-exit at the close of the 8th bar.
  SHORT is the mirror.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

MSK = ZoneInfo("Europe/Moscow")
LOOKBACK = 20
MIN_GAP = 3
STOP_OFF = 0.0002
RR = 2.0
MAX_HOLD = 8
MIN_QV_DEFAULT = 5_000_000
COST = 0.0007
BINGX = "https://open-api.bingx.com"
UA = {"User-Agent": "ict-plus-one/1.0"}

BACKTESTED = {
    "1000PEPEUSDT", "AAVEUSDT", "ADAUSDT", "AGTUSDT", "APTUSDT", "ARBUSDT",
    "ASTERUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ENAUSDT",
    "ETHUSDT", "HYPEUSDT", "INITUSDT", "INJUSDT", "JCTUSDT", "KGENUSDT",
    "LINKUSDT", "LTCUSDT", "NEARUSDT", "ONDOUSDT", "PUMPUSDT", "QNTUSDT",
    "SOLUSDT", "SUIUSDT", "TAOUSDT", "UNIUSDT", "WLDUSDT", "XRPUSDT", "ZECUSDT",
}


def to_bingx(sym: str) -> str:
    s = sym.upper().replace("_", "")
    if "-" in s:
        return s
    if s.endswith("USDT"):
        return s[:-4] + "-USDT"
    return s + "-USDT"


def to_plain(sym: str) -> str:
    return sym.replace("-", "").upper()


def get_json(url, params=None, attempts=4):
    last_err = None
    for attempt in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=25, headers=UA)
            r.raise_for_status()
            j = r.json()
            if j.get("code") in (0, "0", None):
                return j
            last_err = RuntimeError(j.get("msg") or str(j))
        except (requests.RequestException, ValueError) as e:
            last_err = e
        time.sleep(0.6 * (attempt + 1))
    raise last_err or RuntimeError(f"GET {url} failed")


def get_liquid_universe(min_qv: float = MIN_QV_DEFAULT, top_n: int = 0) -> list[dict]:
    c = get_json(f"{BINGX}/openApi/swap/v2/quote/contracts")
    t = get_json(f"{BINGX}/openApi/swap/v2/quote/ticker")
    contracts = c.get("data") or []
    tick = {x["symbol"]: x for x in (t.get("data") or [])}
    rows = []
    rejected = {"nc": 0, "qv": 0, "closed": 0, "not_usdt": 0}
    for x in contracts:
        if x.get("currency") != "USDT" or x.get("status") != 1:
            rejected["not_usdt"] += 1
            continue
        if str(x.get("apiStateOpen")).lower() != "true":
            rejected["closed"] += 1
            continue
        sym, asset = x["symbol"], x.get("asset") or ""
        if sym.startswith("NC") or str(asset).startswith("NC"):
            rejected["nc"] += 1
            continue
        tk = tick.get(sym) or {}
        qv = float(tk.get("quoteVolume") or 0)
        if qv < min_qv:
            rejected["qv"] += 1
            continue
        rows.append(dict(
            bingx=sym, plain=to_plain(sym), qv=qv,
            last=float(tk.get("lastPrice") or 0),
            price_prec=int(x.get("pricePrecision") or 6),
            qty_step=float(x.get("size") or 0) or None,
        ))
    rows.sort(key=lambda r: -r["qv"])
    if top_n and top_n > 0:
        rows = rows[:top_n]
    return rows, rejected, len(contracts)


def fetch_klines(bingx_sym: str, interval: str, limit: int = 200) -> pd.DataFrame:
    j = get_json(
        f"{BINGX}/openApi/swap/v3/quote/klines",
        {"symbol": bingx_sym, "interval": interval, "limit": limit},
    )
    raw = j.get("data") or []
    if not raw:
        raise RuntimeError(f"no klines for {bingx_sym} {interval}")
    rows = []
    for x in raw:
        ts = int(x["time"])
        t = pd.to_datetime(ts, unit="us" if ts > 10**15 else "ms", utc=True)
        rows.append(dict(
            time=t,
            open=float(x["open"]), high=float(x["high"]),
            low=float(x["low"]), close=float(x["close"]),
            volume=float(x.get("volume") or 0),
        ))
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").set_index("time")
    return df[(df.open > 0) & (df.high > 0) & (df.low > 0) & (df.close > 0)]


def drop_unclosed(df: pd.DataFrame, step: pd.Timedelta, now=None) -> pd.DataFrame:
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    return df.loc[df.index + step <= now + pd.Timedelta(seconds=2)].copy()


def plus_one_day1(df: pd.DataFrame, t: int | None = None) -> Optional[dict]:
    """Detect a Plus One Day-1 setup on closed bar t (default: last bar)."""
    if t is None:
        t = len(df) - 1
    if t < LOOKBACK:
        return None
    l = df["low"].to_numpy(float)
    h = df["high"].to_numpy(float)
    c = df["close"].to_numpy(float)
    window_l = l[t - LOOKBACK:t]
    prev_min = float(window_l.min())
    prev_min_i = t - LOOKBACK + int(np.where(window_l == prev_min)[0][-1])
    window_h = h[t - LOOKBACK:t]
    prev_max = float(window_h.max())
    prev_max_i = t - LOOKBACK + int(np.where(window_h == prev_max)[0][-1])

    long_ok = (t - prev_min_i) >= MIN_GAP and l[t] < prev_min and c[t] <= prev_min
    short_ok = (t - prev_max_i) >= MIN_GAP and h[t] > prev_max and c[t] >= prev_max
    if long_ok and short_ok:
        return None
    if long_ok:
        entry = prev_min
        sl = l[t] * (1.0 - STOP_OFF)
        if entry <= sl:
            return None
        tp = entry + RR * (entry - sl)
        return dict(
            direction="long", entry=entry, sl=sl, tp=tp, risk=entry - sl,
            prior_extreme=prev_min, prior_i=int(prev_min_i), gap=int(t - prev_min_i),
            day1_low=float(l[t]), day1_high=float(h[t]), day1_close=float(c[t]),
            setup_i=int(t),
        )
    if short_ok:
        entry = prev_max
        sl = h[t] * (1.0 + STOP_OFF)
        if entry >= sl:
            return None
        tp = entry - RR * (sl - entry)
        return dict(
            direction="short", entry=entry, sl=sl, tp=tp, risk=sl - entry,
            prior_extreme=prev_max, prior_i=int(prev_max_i), gap=int(t - prev_max_i),
            day1_low=float(l[t]), day1_high=float(h[t]), day1_close=float(c[t]),
            setup_i=int(t),
        )
    return None


def simulate_fill_and_exit(df: pd.DataFrame, setup: dict, cost: float = COST) -> Optional[dict]:
    """Next-bar fill if traded through; SL may widen on Day-2; 2R or 8-bar exit."""
    t = setup["setup_i"]
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    n = len(df)
    if t + 1 >= n:
        return dict(status="no_next_bar", filled=False)
    entry = setup["entry"]
    direction = 1 if setup["direction"] == "long" else -1
    if direction == 1:
        if h[t + 1] < entry:
            return dict(status="unfilled", filled=False)
        fill = o[t + 1] if o[t + 1] > entry else entry
        sl = min(l[t], l[t + 1]) * (1.0 - STOP_OFF)
        if fill <= sl:
            return dict(status="invalid_sl", filled=False)
    else:
        if l[t + 1] > entry:
            return dict(status="unfilled", filled=False)
        fill = o[t + 1] if o[t + 1] < entry else entry
        sl = max(h[t], h[t + 1]) * (1.0 + STOP_OFF)
        if fill >= sl:
            return dict(status="invalid_sl", filled=False)
    risk = abs(fill - sl)
    if risk <= 0:
        return dict(status="invalid_sl", filled=False)
    tp = fill + direction * RR * risk
    entry_i = t + 1
    last = min(entry_i + MAX_HOLD - 1, n - 1)
    exit_i, exit_px, reason = last, c[last], "time"
    for j in range(entry_i, last + 1):
        if j == entry_i:
            if direction == 1:
                if (c[j] >= tp) or (o[j] < fill and h[j] >= tp):
                    exit_i, exit_px, reason = j, tp, "tp"
                    break
            else:
                if (c[j] <= tp) or (o[j] > fill and l[j] <= tp):
                    exit_i, exit_px, reason = j, tp, "tp"
                    break
            continue
        if direction == 1:
            if o[j] <= sl:
                exit_i, exit_px, reason = j, o[j], "sl"
                break
            if o[j] >= tp:
                exit_i, exit_px, reason = j, o[j], "tp"
                break
            if l[j] <= sl:
                exit_i, exit_px, reason = j, sl, "sl"
                break
            if h[j] >= tp:
                exit_i, exit_px, reason = j, tp, "tp"
                break
        else:
            if o[j] >= sl:
                exit_i, exit_px, reason = j, o[j], "sl"
                break
            if o[j] <= tp:
                exit_i, exit_px, reason = j, o[j], "tp"
                break
            if h[j] >= sl:
                exit_i, exit_px, reason = j, sl, "sl"
                break
            if l[j] <= tp:
                exit_i, exit_px, reason = j, tp, "tp"
                break
    # if the exit bar is the last *and* we don't have MAX_HOLD bars yet and reason is time,
    # mark as open (incomplete)
    incomplete = reason == "time" and (entry_i + MAX_HOLD - 1) > last and last == n - 1
    gross = (exit_px - fill) * direction
    costs = cost * (fill + exit_px)
    return dict(
        status="open" if incomplete else reason,
        filled=True,
        fill=float(fill),
        sl=float(sl),
        tp=float(tp),
        exit=float(exit_px),
        net_r=float((gross - costs) / risk),
        gross_r=float(gross / risk),
        bars=int(exit_i - entry_i + 1),
        entry_time=str(df.index[entry_i]),
        exit_time=str(df.index[exit_i]),
    )


def fmt_px(x: float, prec: int | None = None) -> str:
    if prec is not None:
        return f"{x:.{prec}f}"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:.2f}"
    if ax >= 1:
        return f"{x:.4f}"
    if ax >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8f}"


def fmt_msk(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(MSK).strftime("%d.%m %H:%M МСК")


def tf_step(tf: str) -> pd.Timedelta:
    return pd.Timedelta(hours=4) if tf == "4h" else pd.Timedelta(days=1)


def annotate_setup(df: pd.DataFrame, setup: dict, tf: str) -> dict:
    step = tf_step(tf)
    last = df.index[setup["setup_i"]]
    setup = dict(setup)
    setup["prior_time"] = str(df.index[setup["prior_i"]])
    setup["setup_time"] = str(last)
    setup["valid_from"] = str(last + step)
    setup["valid_until"] = str(last + 2 * step)
    setup["tf"] = tf
    return setup


def scan_frame(df: pd.DataFrame, tf: str, now=None) -> tuple[pd.DataFrame, Optional[dict]]:
    step = tf_step(tf)
    closed = drop_unclosed(df, step, now=now)
    if len(closed) < LOOKBACK + 2:
        raise RuntimeError(f"only {len(closed)} closed {tf} bars")
    setup = plus_one_day1(closed)
    if setup:
        setup = annotate_setup(closed, setup, tf)
    return closed, setup


def replay_frame(df: pd.DataFrame, tf: str, since, now=None) -> list[dict]:
    """Bar-by-bar Plus One over closed bars with open time >= since."""
    step = tf_step(tf)
    closed = drop_unclosed(df, step, now=now)
    since = pd.Timestamp(since, tz="UTC") if not isinstance(since, pd.Timestamp) else since
    out = []
    for t in range(LOOKBACK, len(closed)):
        if closed.index[t] < since:
            continue
        # need at least the next bar to know fill; include setups anyway
        s = plus_one_day1(closed, t)
        if not s:
            continue
        s = annotate_setup(closed, s, tf)
        outcome = simulate_fill_and_exit(closed, s)
        s["outcome"] = outcome
        out.append(s)
    return out


def liquidity_tags(df: pd.DataFrame, setup: dict) -> list[str]:
    """Context only: which liquidity pools the swept extreme coincides with."""
    t = setup["setup_i"]
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    times = df.index
    tags = ["20-bar pool"]
    level = setup["prior_extreme"]
    # previous UTC day / week
    day_key = np.array([x.year * 10000 + x.month * 100 + x.day for x in times])
    week_key = np.array([x.year * 100 + x.isocalendar()[1] for x in times])

    def prev_sess(keys, arr, mode):
        i = 0
        last = np.nan
        n = len(keys)
        known = np.full(n, np.nan)
        while i < n:
            k = keys[i]
            j = i
            while j < n and keys[j] == k:
                j += 1
            known[i:j] = last
            last = float(np.max(arr[i:j]) if mode == "high" else np.min(arr[i:j]))
            i = j
        return known

    if setup["direction"] == "long":
        pdl = prev_sess(day_key, l, "low")
        pwl = prev_sess(week_key, l, "low")
        if t < len(pdl) and np.isfinite(pdl[t]) and abs(level - pdl[t]) / max(level, 1e-12) < 0.0015:
            tags.append("PDL")
        if t < len(pwl) and np.isfinite(pwl[t]) and abs(level - pwl[t]) / max(level, 1e-12) < 0.0015:
            tags.append("PWL")
    else:
        pdh = prev_sess(day_key, h, "high")
        pwh = prev_sess(week_key, h, "high")
        if t < len(pdh) and np.isfinite(pdh[t]) and abs(level - pdh[t]) / max(level, 1e-12) < 0.0015:
            tags.append("PDH")
        if t < len(pwh) and np.isfinite(pwh[t]) and abs(level - pwh[t]) / max(level, 1e-12) < 0.0015:
            tags.append("PWH")
    return tags


def signal_text(rec: dict, setup: dict, tags: list[str] | None = None) -> str:
    """Plain-text Russian signal. No bot/AI signature."""
    s = setup
    long_ = s["direction"] == "long"
    side = "LONG" if long_ else "SHORT"
    stop_kind = "BUY STOP" if long_ else "SELL STOP"
    prec = rec.get("price_prec")
    risk_pct = 100.0 * s["risk"] / s["entry"]
    qv = rec.get("qv")
    qvs = f", объём 24ч ${qv/1e6:.1f}M" if qv else ""
    flag = "" if rec.get("in_backtest", True) else "  [не было в годовом бэктесте]"
    pools = ", ".join(tags or ["20-bar pool"])
    lines = [
        f"🐢 Turtle Soup Plus One  ·  {s['tf']}",
        f"{'🟢' if long_ else '🔴'} {side}  {rec.get('symbol') or rec.get('plain') or rec.get('bingx')}{flag}",
        "",
        f"Вход ({stop_kind}): {fmt_px(s['entry'], prec)}",
        f"Стоп (предварительный): {fmt_px(s['sl'], prec)}  "
        f"(0.02% за экстремум Day-1; на Day-2 может расшириться)",
        f"Цель 2R: {fmt_px(s['tp'], prec)}",
        f"Риск: {risk_pct:.2f}% цены",
        f"Действителен: {fmt_msk(s['valid_from'])} → {fmt_msk(s['valid_until'])}  (только следующий бар)",
        f"Пул ликвидности: {pools}, уровень {fmt_px(s['prior_extreme'], prec)} "
        f"от {fmt_msk(s['prior_time'])} ({s['gap']} баров назад)",
        f"Day-1 close: {fmt_px(s['day1_close'], prec)}{qvs}",
        "",
        "Исполнять, только если следующий бар прошёл уровень; иначе отмена.",
        "Выход: 2R или закрытие 8-го бара. Не финансовый совет. "
        "Ребро скромное (холдаут 31 монета: +0.18R, PF 1.33).",
    ]
    return "\n".join(lines)
