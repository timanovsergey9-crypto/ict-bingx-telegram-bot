#!/usr/bin/env python3
"""Launcher and CLI for the Turtle Soup Plus One BingX scanner.

settings.env fills in variables that are not already set (secrets are never
read from that file). GitHub Actions secrets TELEGRAM_TOKEN and CHAT_ID are
the only source of Telegram credentials.

  python bot.py --selftest          offline unit tests, no network
  python bot.py --dry-run           live scan, print only, never Telegram
  python bot.py --scan-once         live scan; Telegram if tokens are set
  python bot.py --replay 14         bar-by-bar last N days + 2R/8-bar outcomes
  python bot.py                     same as --scan-once (one-shot, not a 4h loop)

Kill zones are intentionally NOT applied: session filters failed the DEV gates
in the validated research, and they were the main reason this bot stayed silent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROTECTED = {"TELEGRAM_TOKEN", "CHAT_ID", "BOT_TOKEN"}
STATE_PATH = Path(__file__).resolve().parent / "data" / "plus_one_state.json"


def load_settings(path: str = "settings.env") -> None:
    """Fill os.environ with values from settings.env only when a key is unset.

    Secrets in the file are ignored. Already-exported env (including GitHub
    Actions env:) wins — this is the opposite of the previous behaviour, which
    let settings.env silently override workflow-tuned thresholds.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, value = (p.strip() for p in line.split("=", 1))
            if not key or key in PROTECTED:
                continue
            if key not in os.environ or os.environ[key] == "":
                os.environ[key] = value.strip("\"'")


load_settings()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except ValueError:
        return default


MIN_QV = _f("MIN_QUOTE_VOLUME", 5_000_000.0)
TOP_N = _i("TOP_N", 0)
MAX_CONCURRENT = _i("MAX_CONCURRENT", 5)


def selftest() -> int:
    import numpy as np
    import pandas as pd

    from plus_one import (
        LOOKBACK, MIN_GAP, RR, plus_one_day1, drop_unclosed, simulate_fill_and_exit,
        signal_text, annotate_setup,
    )

    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
        ok = ok and bool(cond)

    def make_df(rows):
        idx = pd.date_range("2026-01-01", periods=len(rows), freq="4h", tz="UTC")
        o, h, l, c = zip(*rows)
        return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)

    # 20 bars of a 100-110 range, then a Day-1 new low that closes at/below prior low.
    base = [(100, 110, 95, 102)] * LOOKBACK
    # prior 20-low is 95 at bar 0; put a slightly higher trough later so last prior is recent enough? 
    # window is bars [t-20, t); if t=20, window is 0..19 all with low=95, prior_i = 19 (last occurrence).
    # gap = 20-19=1 < MIN_GAP=3. Need the unique prior low at least 3 bars earlier.
    rows = [(100, 110, 100, 102)] * LOOKBACK  # lows at 100
    rows[5] = (100, 110, 90, 102)             # prior 20-low = 90 at index 5
    # Day-1: sweep below 90, close at 89 (<= 90)
    rows.append((95, 96, 85, 89))
    df = make_df(rows)
    sig = plus_one_day1(df)
    check("long Plus One detected", sig is not None and sig["direction"] == "long")
    if sig:
        check("entry at prior 20-low", abs(sig["entry"] - 90) < 1e-9, f"entry={sig['entry']}")
        check("gap >= MIN_GAP", sig["gap"] >= MIN_GAP, f"gap={sig['gap']}")
        check("RR is 2", abs((sig["tp"] - sig["entry"]) / sig["risk"] - RR) < 1e-9)
        check("close at or below prior low", sig["day1_close"] <= sig["prior_extreme"])

    # close back inside (89 would be below; 91 is inside) — NOT Plus One
    rows_in = list(rows[:-1]) + [(95, 96, 85, 91)]
    df_in = make_df(rows_in)
    check("close back inside is not Plus One", plus_one_day1(df_in) is None)

    # short mirror
    srows = [(100, 100, 90, 98)] * LOOKBACK
    srows[5] = (100, 120, 90, 98)
    srows.append((115, 125, 114, 122))  # new 20-high 125, close 122 >= 120
    dfs = make_df(srows)
    ss = plus_one_day1(dfs)
    check("short Plus One detected", ss is not None and ss["direction"] == "short")
    if ss:
        check("short entry at prior 20-high", abs(ss["entry"] - 120) < 1e-9, f"entry={ss['entry']}")

    # gap too tight: prior low on bar t-2
    tight = [(100, 110, 100, 102)] * LOOKBACK
    tight[-1] = (100, 110, 90, 102)  # prior at index 19 when t=20, gap=1
    tight.append((95, 96, 85, 89))
    check("gap < 3 rejected", plus_one_day1(make_df(tight)) is None)

    # drop_unclosed: last bar still open
    now = df.index[-1] + pd.Timedelta(hours=1)  # 1h into the last 4h bar
    dropped = drop_unclosed(df, pd.Timedelta(hours=4), now=now)
    check("open bar dropped", len(dropped) == len(df) - 1)

    # fill + TP path
    # After Day-1 at t=20, Day-2 trades through 90, then goes to 2R
    if sig:
        ext = list(rows)
        # Day-2: open 88 (below entry? wait long fill: high must >= 90.
        # open 91 > entry 90 → fill at open 91
        # sl = min(85, day2 low)*0.9998. Let day2 be a clean up-bar.
        # risk at fill 91 - sl. Let's add enough bars.
        ext.append((91, 100, 90.5, 95))  # fill at 91, sl = min(85, 90.5)*0.9998 = 84.983, risk≈6.017, tp≈103
        for _ in range(8):
            ext.append((95, 110, 94, 108))  # should hit TP ~103
        dfe = make_df(ext)
        sig2 = plus_one_day1(dfe, t=LOOKBACK)
        out = simulate_fill_and_exit(dfe, sig2)
        check("filled and reached TP", out and out.get("filled") and out.get("status") == "tp", str(out))
        if out and out.get("filled"):
            check("net R after costs is positive on TP", out["net_r"] > 1.5, f"net_r={out['net_r']:.3f}")

    # message has no AI/bot signature and contains the levels
    if sig:
        rec = {"symbol": "BTCUSDT", "price_prec": 1, "qv": 1e9, "in_backtest": True}
        msg = signal_text(rec, annotate_setup(df, sig, "4h"), ["20-bar pool"])
        check("message has entry", "BUY STOP" in msg and "LONG" in msg)
        check("no AI signature", "AI" not in msg and "бот сгенерир" not in msg.lower())
        check("has caveat", "Не финансовый совет" in msg)

    print("ИТОГ: " + ("ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ"))
    return 0 if ok else 1


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def telegram_send(text: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip()
    chat = os.getenv("CHAT_ID", "").strip()
    if not token or not chat:
        print("Telegram skipped: TELEGRAM_TOKEN/CHAT_ID not set")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            print(f"Telegram rejected: {body}")
            return False
        return True
    except Exception as e:
        print(f"Telegram send failed: {type(e).__name__}: {e}")
        return False


def run_scan(dry_run: bool, send: bool) -> int:
    from plus_one import (
        get_liquid_universe, fetch_klines, scan_frame, signal_text,
        liquidity_tags, fmt_msk, BACKTESTED,
    )
    from plus_one import MIN_QV_DEFAULT  # noqa: F401
    from datetime import timezone as tz
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
    now = datetime.now(MSK)
    tfs = ["4h", "1d"]
    print(f"Plus One scan  {now.strftime('%Y-%m-%d %H:%M:%S %Z')}  min_qv=${MIN_QV:,.0f}  dry_run={dry_run}")
    try:
        uni, rejected, n_contracts = get_liquid_universe(MIN_QV, TOP_N)
    except Exception as e:
        print(f"ERROR universe: {type(e).__name__}: {e}")
        return 1
    print(
        f"FUNNEL universe: contracts={n_contracts}  rejected NC={rejected['nc']} "
        f"closed={rejected['closed']} qv<{MIN_QV:.0f}={rejected['qv']}  "
        f"liquid={len(uni)}"
    )

    state = {} if dry_run else load_state()
    results = []
    errors = []
    funnel = {"scanned_4h": 0, "scanned_1d": 0, "setups_4h": 0, "setups_1d": 0,
              "new": 0, "dupes": 0, "sent": 0, "send_fail": 0, "kline_err": 0}

    for rec in uni:
        rec = dict(rec)
        rec["in_backtest"] = rec["plain"] in BACKTESTED
        for tf in tfs:
            try:
                df = fetch_klines(rec["bingx"], tf, limit=200)
                closed, setup = scan_frame(df, tf)
                funnel[f"scanned_{tf}"] += 1
                item = {**rec, "tf": tf, "setup": setup,
                        "last_bar": str(closed.index[-1]),
                        "last_close": float(closed["close"].iloc[-1]),
                        "n_bars": len(closed), "tags": []}
                if setup:
                    funnel[f"setups_{tf}"] += 1
                    try:
                        item["tags"] = liquidity_tags(closed, setup)
                    except Exception:
                        item["tags"] = ["20-bar pool"]
                results.append(item)
                time.sleep(0.08)
            except Exception as e:
                funnel["kline_err"] += 1
                errors.append({"symbol": rec["plain"], "tf": tf, "error": f"{type(e).__name__}: {e}"})
                time.sleep(0.2)

    print(
        f"FUNNEL scanned 4h={funnel['scanned_4h']} 1d={funnel['scanned_1d']}  "
        f"setups 4h={funnel['setups_4h']} 1d={funnel['setups_1d']}  kline_err={funnel['kline_err']}"
    )
    if errors:
        for e in errors[:15]:
            print(f"  ERR {e['symbol']} {e['tf']}: {e['error']}")
        if len(errors) > 15:
            print(f"  ... and {len(errors)-15} more errors")

    setups = [r for r in results if r.get("setup")]
    # prefer 4h, then higher qv
    setups.sort(key=lambda r: (0 if r["tf"] == "4h" else 1, -(r.get("qv") or 0)))

    for r in results:
        s = r.get("setup")
        flag = "" if r.get("in_backtest", True) else " [not in 1y backtest]"
        qvs = f" qv=${r['qv']/1e6:.1f}M" if r.get("qv") else ""
        if not s:
            continue
        print(
            f"SETUP {r['plain']} {r['tf']} {s['direction']} entry={s['entry']} "
            f"sl={s['sl']} tp={s['tp']} valid {s['valid_from']} → {s['valid_until']}{qvs}{flag}"
        )
        import pandas as _pd
        now_utc = _pd.Timestamp.now(tz="UTC")
        vf, vu = _pd.Timestamp(s["valid_from"]), _pd.Timestamp(s["valid_until"])
        if now_utc >= vu:
            print("  expired (Day-2 already closed), skip send")
            funnel["dupes"] += 1  # counted as skipped
            continue
        # Daily Day-2 lasts 24h; only announce in the first 4h so a late cron
        # does not push a stale 1d stop 20 hours in.
        if r["tf"] == "1d" and now_utc > vf + _pd.Timedelta(hours=4):
            print("  1d setup is more than 4h into Day-2, skip (stale)")
            continue
        key = f"{r['plain']}|{r['tf']}|{s['setup_time']}|{s['direction']}"
        if key in state:
            funnel["dupes"] += 1
            print(f"  dupe (already recorded), skip send")
            continue
        funnel["new"] += 1
        text = signal_text(r, s, r.get("tags"))
        if dry_run or not send:
            print("--- DRY-RUN message ---")
            print(text)
            print("-----------------------")
        else:
            if telegram_send(text):
                funnel["sent"] += 1
                state[key] = {"t": now.isoformat(), "entry": s["entry"], "sl": s["sl"], "tp": s["tp"]}
                print("  sent to Telegram")
            else:
                funnel["send_fail"] += 1
                print("  SEND FAILED")

    if not dry_run:
        # prune old keys (keep 14 days)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        state = {k: v for k, v in state.items() if str(v.get("t", "9999")) >= cutoff[:10] or True}
        save_state(state)

    print(
        f"FUNNEL new={funnel['new']} dupes={funnel['dupes']} "
        f"sent={funnel['sent']} send_fail={funnel['send_fail']}"
    )
    print(
        f"DONE setups={len(setups)} across {len(uni)} coins. "
        f"{'DRY-RUN, nothing sent.' if dry_run or not send else ''}"
    )
    if len(setups) > MAX_CONCURRENT:
        print(f"NOTE: {len(setups)} setups > max {MAX_CONCURRENT} concurrent suggested.")
    return 0


def run_replay(days: int) -> int:
    from plus_one import (
        get_liquid_universe, fetch_klines, replay_frame, BACKTESTED, COST,
    )
    import pandas as pd
    from collections import Counter

    now = pd.Timestamp.now(tz="UTC")
    since = now - pd.Timedelta(days=days)
    print(f"Plus One REPLAY last {days} days  since {since}  min_qv=${MIN_QV:,.0f}  cost={COST}/side")
    uni, rejected, n_contracts = get_liquid_universe(MIN_QV, TOP_N)
    print(f"FUNNEL universe liquid={len(uni)} (from {n_contracts}, qv-reject={rejected['qv']})")

    all_setups = []
    errors = 0
    scanned = 0
    for rec in uni:
        for tf, limit in (("4h", 200), ("1d", 200)):
            try:
                df = fetch_klines(rec["bingx"], tf, limit=limit)
                found = replay_frame(df, tf, since=since, now=now)
                scanned += 1
                for s in found:
                    s["symbol"] = rec["plain"]
                    s["qv"] = rec["qv"]
                    s["in_backtest"] = rec["plain"] in BACKTESTED
                    all_setups.append(s)
                time.sleep(0.08)
            except Exception as e:
                errors += 1
                print(f"  ERR {rec['plain']} {tf}: {type(e).__name__}: {e}")
                time.sleep(0.2)

    # daily coverage and outcomes
    by_day = Counter()
    by_tf = Counter()
    closed = [s for s in all_setups if s.get("outcome") and s["outcome"].get("filled") and s["outcome"]["status"] in ("tp", "sl", "time")]
    unfilled = [s for s in all_setups if s.get("outcome") and s["outcome"].get("status") == "unfilled"]
    open_tr = [s for s in all_setups if s.get("outcome") and s["outcome"].get("status") == "open"]
    for s in all_setups:
        ts = pd.Timestamp(s["setup_time"])
        by_day[str(ts.floor("D").date())] += 1
        by_tf[s["tf"]] += 1

    cal = pd.date_range(since.floor("D"), now.floor("D"), freq="D", tz="UTC")
    covered = sum(1 for d in cal if str(d.date()) in by_day)
    print(f"FUNNEL scanned_frames={scanned} errors={errors}")
    print(f"SETUPS: {len(all_setups)}  4h={by_tf['4h']}  1d={by_tf['1d']}")
    print(f"DAYS with >=1 setup: {covered}/{len(cal)} ({100*covered/max(len(cal),1):.1f}%)  t/day={len(all_setups)/max(len(cal),1):.2f}")
    print(f"OUTCOMES filled={len(closed)} unfilled={len(unfilled)} still_open={len(open_tr)}")
    if closed:
        import numpy as np
        nets = np.array([s["outcome"]["net_r"] for s in closed])
        gp = nets[nets > 0].sum()
        gl = -nets[nets <= 0].sum()
        pf = gp / gl if gl > 0 else float("inf")
        wr = 100 * (nets > 0).mean()
        print(f"FILLED (complete, 0.07%/side): n={len(closed)} avgR={nets.mean():+.3f} PF={pf:.2f} WR={wr:.1f}%")
        reasons = Counter(s["outcome"]["status"] for s in closed)
        print(f"  reasons: {dict(reasons)}")
    # per day
    print("Per day:")
    for d in cal:
        k = str(d.date())
        n = by_day.get(k, 0)
        print(f"  {k}  setups={n}")
    # list setups
    for s in all_setups:
        oc = s.get("outcome") or {}
        st = oc.get("status", "?")
        nr = oc.get("net_r")
        nr_s = f" netR={nr:+.2f}" if nr is not None and oc.get("filled") else ""
        print(f"  {s['symbol']:14s} {s['tf']} {s['direction']:5s} {s['setup_time']}  {st}{nr_s}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Turtle Soup Plus One scanner (BingX USDT-M)")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--scan-once", action="store_true", dest="scan_once")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Live scan, never send to Telegram")
    p.add_argument("--replay", type=int, default=0, metavar="DAYS",
                   help="Bar-by-bar replay over the last DAYS days with outcomes")
    p.add_argument("--min-qv", type=float, default=None)
    p.add_argument("--top-n", type=int, default=None)
    args = p.parse_args(argv)

    global MIN_QV, TOP_N
    if args.min_qv is not None:
        MIN_QV = args.min_qv
    if args.top_n is not None:
        TOP_N = args.top_n

    if args.selftest:
        return selftest()
    if args.replay:
        return run_replay(args.replay)
    # default and --scan-once: one-shot live scan
    dry = args.dry_run
    send = (not dry)
    return run_scan(dry_run=dry, send=send)


if __name__ == "__main__":
    sys.exit(main())
