#!/usr/bin/env python3
"""
ICT / Smart Money бот для BingX (топ ликвидных пар) -> сигналы в Telegram.

Сигнал выдаётся только если ВСЕ условия выполнены (на ЗАКРЫТЫХ свечах):
  1. Сейчас активна Kill Zone (время Нью-Йорка, DST учитывается), и сама
     свеча-свип тоже была внутри Kill Zone.
  2. HTF-bias по структуре (HH+HL = bullish, LH+LL = bearish), а не по EMA.
  3. Premium/Discount: вход LONG ниже EQ дилинг-рейнджа HTF, SHORT выше EQ.
  4. Sweep ликвидности: swing low/high на LTF или PDL/PDH (вынос тенью + возврат).
  5. MSS: закрытие за последним внутренним swing с displacement
     (тело >= DISP_ATR*ATR и доля тела >= DISP_BODY_RATIO).
  6. FVG, образованный именно displacement-свечой этого движения.
  7. FVG не митигирован: цена ещё не касалась CE (иначе лимитка уже сработала).
  8. Стоп за экстремумом свипа (+ буфер ATR), риск ограничен в ATR.
  9. Цель (TP1) - ближайшая нетронутая ликвидность, RR >= MIN_RR.
 10. Фильтр BTC: альт-сигнал против структуры BTC отбрасывается.
 11. Лимит сигналов в день, кулдаун по паре, дедупликация (переживает перезапуск).

Сделки отслеживаются по 1m-свечам (high/low, а не last price):
pending -> active (коснулись CE) -> win / loss / timeout;
или expired / missed, если вход так и не случился. Консервативно: если в одной
свече задеты и стоп, и тейк - считается стоп.

Не является финансовой рекомендацией.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# ============================== CONFIG ==============================


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HTF = os.getenv("HTF", "4h")
LTF = os.getenv("LTF", "15m")
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap")  # swap = фьючерсы (long+short), spot = только long
ALLOW_SHORT = MARKET_TYPE != "spot"
TOP_N = _i("TOP_N", 20)
MIN_QUOTE_VOLUME = _f("MIN_QUOTE_VOLUME", 5_000_000)  # USDT за 24ч
MIN_RR = _f("MIN_RR", 2.0)
MAX_SIGNALS_PER_DAY = _i("MAX_SIGNALS_PER_DAY", 2)
COOLDOWN_H = _f("COOLDOWN_H", 4)
BTC_FILTER = os.getenv("BTC_FILTER", "1") == "1"
ENABLED_KZ = [z.strip() for z in os.getenv("KILL_ZONES", "London,New York").split(",") if z.strip()]

# Параметры ICT-логики
SWING_N = _i("SWING_N", 3)               # фрактал на LTF
HTF_SWING = _i("HTF_SWING", 3)           # фрактал на HTF
SWEEP_LOOKBACK = _i("SWEEP_LOOKBACK", 12)  # как давно мог быть свип (в свечах LTF)
MSS_WINDOW = _i("MSS_WINDOW", 6)         # сколько свечей после свипа ждём MSS
MAX_FVG_AGE = _i("MAX_FVG_AGE", 8)       # сигнал свежий, пока FVG не старше N свечей
REF_LOOKBACK = _i("REF_LOOKBACK", 60)
LEVEL_LOOKBACK = _i("LEVEL_LOOKBACK", 150)
DISP_ATR = _f("DISP_ATR", 1.5)
DISP_BODY_RATIO = _f("DISP_BODY_RATIO", 0.55)
FVG_MIN_ATR = _f("FVG_MIN_ATR", 0.3)
STOP_BUFFER_ATR = _f("STOP_BUFFER_ATR", 0.15)
MIN_RISK_ATR = _f("MIN_RISK_ATR", 0.5)
MAX_RISK_ATR = _f("MAX_RISK_ATR", 5.0)

# Сопровождение сделок
SETUP_TTL_MIN = _i("SETUP_TTL_MIN", 240)    # сколько ждём касания CE
TRADE_MAX_HOURS = _i("TRADE_MAX_HOURS", 48)  # максимум жизни активной сделки
MONITOR_INTERVAL = _i("MONITOR_INTERVAL", 60)
CLOSE_DELAY_S = _i("CLOSE_DELAY_S", 8)       # пауза после закрытия свечи перед сканом
KZ_ALERT_MIN = _i("KZ_ALERT_MIN", 15)
CHART_BARS = _i("CHART_BARS", 90)

DATA_DIR = os.getenv("DATA_DIR", "data")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ICT")

# ============================== TIME / KILL ZONES ==============================

NY = ZoneInfo("America/New_York")
MSK = timezone(timedelta(hours=3))
# Часы по Нью-Йорку (стандартные ICT Kill Zones). Asia по умолчанию выключена.
KZ_HOURS_NY = {"Asia": (20, 24), "London": (2, 5), "New York": (7, 10)}

_TF_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def tf_seconds(tf: str) -> int:
    return int(tf[:-1]) * _TF_UNITS[tf[-1]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fmt_msk(dt) -> str:
    return pd.Timestamp(dt).tz_convert(MSK).strftime("%H:%M МСК")


def kz_at(dt) -> Optional[str]:
    hour = pd.Timestamp(dt).tz_convert(NY).hour
    for name in ENABLED_KZ:
        rng = KZ_HOURS_NY.get(name)
        if rng and rng[0] <= hour < rng[1]:
            return name
    return None


def next_kz_start(now: datetime) -> Optional[Tuple[str, datetime]]:
    now_ny = now.astimezone(NY)
    best = None
    for name in ENABLED_KZ:
        rng = KZ_HOURS_NY.get(name)
        if not rng:
            continue
        start = now_ny.replace(hour=rng[0] % 24, minute=0, second=0, microsecond=0)
        if start <= now_ny:
            start += timedelta(days=1)
        start_utc = start.astimezone(timezone.utc)
        if best is None or start_utc < best[1]:
            best = (name, start_utc)
    return best


# ============================== INDICATORS / STRUCTURE ==============================


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def swing_points(df: pd.DataFrame, left: int, right: int):
    """Подтверждённые фрактальные swing'и: [(позиция, цена)], right свечей уже справа."""
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    for i in range(left, len(df) - right):
        if h[i] > h[i - left:i].max() and h[i] >= h[i + 1:i + right + 1].max():
            highs.append((i, float(h[i])))
        if l[i] < l[i - left:i].min() and l[i] <= l[i + 1:i + right + 1].min():
            lows.append((i, float(l[i])))
    return highs, lows


def prev_day_levels(df: pd.DataFrame) -> Optional[Tuple[float, float, int]]:
    """(PDH, PDL, позиция начала текущего дня) по календарю Нью-Йорка."""
    days = df.index.tz_convert(NY).normalize()
    today = days[-1]
    prev_mask = np.asarray(days < today)
    if not prev_mask.any():
        return None
    prev_day = days[prev_mask].max()
    sel = df[np.asarray(days == prev_day)]
    pos_today = int(np.argmax(np.asarray(days == today)))
    return float(sel["high"].max()), float(sel["low"].min()), pos_today


def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """Зеркалим цены, чтобы SHORT-логика == LONG-логика (меньше кода - меньше багов)."""
    return pd.DataFrame(
        {
            "open": -df["open"],
            "high": -df["low"],
            "low": -df["high"],
            "close": -df["close"],
            "volume": df["volume"],
        },
        index=df.index,
    )


def htf_context(df: pd.DataFrame) -> Optional[Dict]:
    """Структурный bias + дилинг-рейндж + HTF-ликвидность. df - только закрытые свечи."""
    if len(df) < 60:
        return None
    highs, lows = swing_points(df, HTF_SWING, HTF_SWING)
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl:
        bias = "bullish"
    elif lh and ll:
        bias = "bearish"
    else:
        bias = "neutral"
    rng_hi, rng_lo = highs[-1][1], lows[-1][1]
    if rng_hi <= rng_lo:
        rng_hi, rng_lo = float(df["high"].iloc[-60:].max()), float(df["low"].iloc[-60:].min())
    return {
        "bias": bias,
        "range_high": rng_hi,
        "range_low": rng_lo,
        "eq": (rng_hi + rng_lo) / 2,
        "htf_highs": [v for _, v in highs[-6:]],
        "htf_lows": [v for _, v in lows[-6:]],
    }


# ============================== ICT CORE ==============================


def _core_long(
    df: pd.DataFrame,
    sweep_levels: List[Tuple[int, float, str]],
    target_levels: List[Tuple[float, str]],
    sweep_time_ok: Optional[Callable] = None,
) -> Optional[Dict]:
    """Sweep sell-side -> MSS(displacement) -> bullish FVG -> лимитный вход в CE."""
    n = len(df)
    if n < 60:
        return None
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    a = atr(df).to_numpy()
    last = n - 1
    if np.isnan(a[last]):
        return None
    highs, _ = swing_points(df, SWING_N, SWING_N)

    for k in range(last, max(last - SWEEP_LOOKBACK, 30) - 1, -1):
        if np.isnan(a[k]):
            continue
        if sweep_time_ok and not sweep_time_ok(df.index[k]):
            continue

        # 1) Sweep: прокол уровня тенью и возврат закрытием выше него
        swept = []
        for p, lvl, lab in sweep_levels:
            if p >= k or not (l[k] < lvl < c[k]):
                continue
            between = l[p + 1:k]
            if between.size and between.min() < lvl:
                continue  # уровень уже был снят раньше
            swept.append((p, lvl, lab))
        if not swept:
            continue
        sp, slvl, slab = min(swept, key=lambda s: s[1])

        # 2) MSS: закрытие выше последнего внутреннего swing high до свипа
        refs = [(hp, hv) for hp, hv in highs if k - REF_LOOKBACK <= hp < k and hv > slvl]
        if not refs:
            continue
        rp, rv = refs[-1]
        m = next((j for j in range(k, min(k + MSS_WINDOW, last) + 1) if c[j] > rv), None)
        if m is None:
            continue
        body, rng = c[m] - o[m], h[m] - l[m]
        if body < DISP_ATR * a[m] or rng <= 0 or body / rng < DISP_BODY_RATIO:
            continue  # пробой есть, но без displacement

        extreme = float(l[k:m + 1].min())

        # 3) FVG, созданный displacement-движением (средняя свеча внутри ноги k..m)
        fvgs = []
        for i in range(max(k + 1, 2), last + 1):
            mid = i - 1
            if mid < k or mid > m or c[mid] <= o[mid]:
                continue
            bottom, top = float(h[i - 2]), float(l[i])
            if top <= bottom or (top - bottom) < FVG_MIN_ATR * a[mid]:
                continue
            ce = (top + bottom) / 2
            after = l[i + 1:]
            if after.size and after.min() <= ce:
                continue  # CE уже затронут - лимитка бы исполнилась, сигнал запоздал
            if last - i > MAX_FVG_AGE:
                continue
            fvgs.append({"i": i, "bottom": bottom, "top": top, "ce": ce, "size": top - bottom})
        if not fvgs:
            continue
        fvg = max(fvgs, key=lambda f: (f["size"], f["i"]))

        # 4) Стоп за экстремумом свипа, ограничение риска
        entry = fvg["ce"]
        stop = extreme - STOP_BUFFER_ATR * a[last]
        risk = entry - stop
        if risk < MIN_RISK_ATR * a[last] or risk > MAX_RISK_ATR * a[last]:
            continue

        # 5) Цель: ближайшая НЕтронутая ликвидность выше
        high_since = float(h[k:].max())
        cands = sorted((t for t in target_levels if t[0] > max(high_since, entry)), key=lambda t: t[0])
        if not cands:
            continue
        tp1, tp1_lab = cands[0]
        rr = (tp1 - entry) / risk
        if rr < MIN_RR:
            continue
        tp2 = next(((p2, l2) for p2, l2 in cands[1:] if p2 > tp1 + 0.5 * a[last]), None)

        return {
            "k": k, "m": m, "ref_pos": rp, "ref": float(rv),
            "level_pos": sp, "level": float(slvl), "level_label": slab,
            "extreme": extreme,
            "fvg_i": fvg["i"], "fvg_bottom": fvg["bottom"], "fvg_top": fvg["top"],
            "entry": float(entry), "stop": float(stop),
            "tp1": float(tp1), "tp1_label": tp1_lab,
            "tp2": float(tp2[0]) if tp2 else None, "tp2_label": tp2[1] if tp2 else None,
            "rr": float(rr), "atr": float(a[last]), "sweep_ts": df.index[k],
        }
    return None


def _unmirror(r: Dict) -> Dict:
    out = dict(r)
    for key in ("ref", "level", "extreme", "entry", "stop", "tp1"):
        out[key] = -r[key]
    out["tp2"] = -r["tp2"] if r["tp2"] is not None else None
    out["fvg_bottom"], out["fvg_top"] = -r["fvg_top"], -r["fvg_bottom"]
    return out


def find_setup(df: pd.DataFrame, side: str, ctx: Dict, sweep_time_ok: Optional[Callable] = None) -> Optional[Dict]:
    highs, lows = swing_points(df, SWING_N, SWING_N)
    lb = len(df) - LEVEL_LOOKBACK
    pd_levels = prev_day_levels(df)

    if side == "long":
        sweeps = [(p, v, "swing low") for p, v in lows if p >= lb]
        targets = [(v, "swing high") for p, v in highs if p >= lb]
        targets += [(v, "HTF high") for v in ctx["htf_highs"]]
        if pd_levels:
            pdh, pdl, day_pos = pd_levels
            sweeps.append((day_pos, pdl, "PDL"))
            targets.append((pdh, "PDH"))
        return _core_long(df, sweeps, targets, sweep_time_ok)

    sweeps = [(p, -v, "swing high") for p, v in highs if p >= lb]
    targets = [(-v, "swing low") for p, v in lows if p >= lb]
    targets += [(-v, "HTF low") for v in ctx["htf_lows"]]
    if pd_levels:
        pdh, pdl, day_pos = pd_levels
        sweeps.append((day_pos, -pdh, "PDH"))
        targets.append((-pdl, "PDL"))
    res = _core_long(mirror(df), sweeps, targets, sweep_time_ok)
    return _unmirror(res) if res else None


def analyze_frames(
    symbol: str,
    df_htf: pd.DataFrame,
    df_ltf: pd.DataFrame,
    now: datetime,
    btc_bias: Optional[str] = None,
    is_btc: bool = False,
) -> Optional[Dict]:
    """Чистая функция (без сети): все фильтры подряд. Фреймы - только закрытые свечи."""
    kz = kz_at(now)
    if not kz or len(df_htf) < 60 or len(df_ltf) < 120:
        return None

    ctx = htf_context(df_htf)
    if not ctx or ctx["bias"] == "neutral":
        return None
    side = "long" if ctx["bias"] == "bullish" else "short"
    if side == "short" and not ALLOW_SHORT:
        return None
    if BTC_FILTER and not is_btc and btc_bias in ("bullish", "bearish") and btc_bias != ctx["bias"]:
        return None  # альт против структуры BTC

    s = find_setup(df_ltf, side, ctx, lambda ts: kz_at(ts) is not None)
    if not s:
        return None

    # Premium / Discount по дилинг-рейнджу HTF
    if side == "long" and s["entry"] >= ctx["eq"]:
        return None
    if side == "short" and s["entry"] <= ctx["eq"]:
        return None

    view = df_ltf.iloc[-CHART_BARS:]
    direction = side.upper()
    return {
        **s,
        "symbol": symbol,
        "direction": direction,
        "kill_zone": kz,
        "htf_bias": ctx["bias"],
        "eq": ctx["eq"],
        "range_high": ctx["range_high"],
        "range_low": ctx["range_low"],
        "zone": "discount" if side == "long" else "premium",
        "key": f"{symbol}|{direction}|{s['sweep_ts'].isoformat()}",
        "df": view,
        "offset": len(df_ltf) - len(view),
    }


# ============================== EXCHANGE ==============================

STABLES = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "BUSD", "USDP", "PYUSD", "USD1"}


def make_exchange():
    import ccxt

    return ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": MARKET_TYPE}})


def drop_unclosed(df: pd.DataFrame, tf: str, now: Optional[datetime] = None) -> pd.DataFrame:
    if df.empty:
        return df
    now = now or utcnow()
    if df.index[-1] + pd.Timedelta(seconds=tf_seconds(tf)) > now:
        return df.iloc[:-1]
    return df


def fetch_frame(ex, symbol: str, tf: str, limit: int) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, tf, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return drop_unclosed(df, tf)


def fetch_top_symbols(ex) -> List[str]:
    fallback = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"] if MARKET_TYPE != "spot" else ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    try:
        rows = []
        for sym, t in ex.fetch_tickers().items():
            if MARKET_TYPE == "spot":
                if not sym.endswith("/USDT") or ":" in sym:
                    continue
            elif not sym.endswith("/USDT:USDT"):
                continue
            base = sym.split("/")[0]
            if base in STABLES or re.search(r"\d+[LS]$", base):
                continue
            vol = t.get("quoteVolume") or 0
            if vol >= MIN_QUOTE_VOLUME:
                rows.append((sym, vol))
        rows.sort(key=lambda r: r[1], reverse=True)
        return [s for s, _ in rows[:TOP_N]] or fallback
    except Exception as e:  # noqa: BLE001
        logger.error("fetch_top_symbols: %s", e)
        return fallback


def btc_symbol() -> str:
    return "BTC/USDT" if MARKET_TYPE == "spot" else "BTC/USDT:USDT"


def get_btc_bias(ex) -> Optional[str]:
    try:
        ctx = htf_context(fetch_frame(ex, btc_symbol(), HTF, 250))
        return ctx["bias"] if ctx else None
    except Exception as e:  # noqa: BLE001
        logger.warning("btc bias: %s", e)
        return None


def analyze_symbol(ex, symbol: str, btc_bias: Optional[str]) -> Optional[Dict]:
    df_htf = fetch_frame(ex, symbol, HTF, 250)
    df_ltf = fetch_frame(ex, symbol, LTF, 300)
    return analyze_frames(symbol, df_htf, df_ltf, utcnow(), btc_bias, is_btc=(symbol == btc_symbol()))


# ============================== TRADES ==============================


class TradeStore:
    def __init__(self, path: str):
        self.path = path
        self.trades: List[Dict] = []

    def load(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.trades = json.load(f)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.trades, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, trade: Dict) -> None:
        self.trades.append(trade)
        self.save()

    def signals_today(self, now: datetime) -> int:
        day = now.date()
        return sum(1 for t in self.trades if datetime.fromtimestamp(t["created_ms"] / 1000, timezone.utc).date() == day)

    def blocked(self, sig: Dict, now: datetime) -> bool:
        for t in self.trades:
            if t["key"] == sig["key"]:
                return True
            if t["symbol"] == sig["symbol"]:
                if t["status"] in ("pending", "active"):
                    return True
                if ms(now) - t["created_ms"] < COOLDOWN_H * 3600 * 1000:
                    return True
        return False


def make_trade(sig: Dict, now: datetime) -> Dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "key": sig["key"],
        "symbol": sig["symbol"],
        "direction": sig["direction"],
        "entry": sig["entry"],
        "stop": sig["stop"],
        "tp1": sig["tp1"],
        "tp2": sig["tp2"],
        "rr": round(sig["rr"], 2),
        "status": "pending",
        "created_ms": ms(now),
        "expires_ms": ms(now) + SETUP_TTL_MIN * 60_000,
        "checked_ms": ms(now),
        "filled_ms": None,
        "closed_ms": None,
        "r": None,
    }


def evaluate_trade(t: Dict, candles: List[Tuple[int, float, float, float, float]]) -> List[str]:
    """Прогоняет закрытые 1m-свечи (ts, o, h, l, c) по сделке. Меняет t, возвращает события."""
    events: List[str] = []
    s = 1 if t["direction"] == "LONG" else -1
    entry, stop, tp = s * t["entry"], s * t["stop"], s * t["tp1"]
    risk = entry - stop

    def close(status: str, r: float, ts: int) -> None:
        t["status"], t["r"], t["closed_ms"] = status, round(r, 2), ts
        events.append(status)

    for ts, _o, h, l, c in candles:
        if ts <= t["checked_ms"]:
            continue
        t["checked_ms"] = ts
        lo, hi, cl = (l, h, c) if s == 1 else (-h, -l, -c)

        if t["status"] == "pending":
            if ts >= t["expires_ms"]:
                close("expired", 0.0, ts)
                break
            if lo <= entry:
                t["status"], t["filled_ms"] = "active", ts
                events.append("filled")
                if lo <= stop:
                    close("loss", -1.0, ts)  # в одной свече вход и стоп - считаем стоп
                    break
                if cl >= tp:
                    close("win", (tp - entry) / risk, ts)
                    break
            elif hi >= tp:
                close("missed", 0.0, ts)  # цена ушла к цели без нашего входа
                break
        elif t["status"] == "active":
            if lo <= stop:
                close("loss", -1.0, ts)
                break
            if hi >= tp:
                close("win", (tp - entry) / risk, ts)
                break
            if ts - (t["filled_ms"] or ts) > TRADE_MAX_HOURS * 3_600_000:
                close("timeout", (cl - entry) / risk, ts)
                break
    return events


def stats_text(store: TradeStore) -> str:
    ts = store.trades

    def cnt(*st: str) -> int:
        return sum(1 for t in ts if t["status"] in st)

    wins, losses, timeouts = cnt("win"), cnt("loss"), cnt("timeout")
    total_r = sum(t["r"] for t in ts if t.get("r") is not None)
    decided = wins + losses
    wr = f"{wins / decided * 100:.0f}%" if decided else "-"
    return (
        "📊 <b>Статистика</b>\n\n"
        f"Сигналов: {len(ts)}\n"
        f"Ждут входа: {cnt('pending')} | В сделке: {cnt('active')}\n"
        f"Не исполнились: {cnt('expired', 'missed')}\n\n"
        f"✅ TP: {wins} | ❌ SL: {losses} | ⏱ Таймаут: {timeouts}\n"
        f"Winrate (TP/(TP+SL)): {wr}\n"
        f"Итого: <b>{total_r:+.2f}R</b>"
    )


# ============================== MESSAGES / CHART ==============================


def fp(p: float) -> str:
    d = 2 if p >= 1000 else 4 if p >= 1 else 5 if p >= 0.01 else 8
    return f"{p:.{d}f}"


def signal_text(sig: Dict) -> str:
    long_ = sig["direction"] == "LONG"
    expire_at = utcnow() + timedelta(minutes=SETUP_TTL_MIN)
    structure = "бычья (HH/HL)" if long_ else "медвежья (LH/LL)"
    zone = "Discount (ниже EQ)" if long_ else "Premium (выше EQ)"
    swept = "sell-side" if long_ else "buy-side"
    brk = "выше" if long_ else "ниже"
    tp2 = f"TP2: <code>{fp(sig['tp2'])}</code> ({sig['tp2_label']})\n" if sig.get("tp2") else ""
    return (
        f"{'🟢' if long_ else '🔴'} <b>ICT SIGNAL: {sig['symbol']} {sig['direction']}</b>\n\n"
        f"<b>Контекст</b>\n"
        f"• Kill Zone: {sig['kill_zone']} ({fmt_msk(utcnow())})\n"
        f"• HTF {HTF}: структура {structure}, цена в {zone}, EQ {fp(sig['eq'])}\n"
        f"• Снята {swept} ликвидность: {sig['level_label']} {fp(sig['level'])} ({fmt_msk(sig['sweep_ts'])})\n"
        f"• MSS: закрытие {brk} {fp(sig['ref'])} с displacement\n"
        f"• FVG {LTF}: {fp(sig['fvg_bottom'])} – {fp(sig['fvg_top'])}\n\n"
        f"<b>План (лимитный вход по {LTF})</b>\n"
        f"Вход (CE): <code>{fp(sig['entry'])}</code>\n"
        f"Стоп: <code>{fp(sig['stop'])}</code> (за экстремумом свипа)\n"
        f"TP1: <code>{fp(sig['tp1'])}</code> ({sig['tp1_label']}), RR 1:{sig['rr']:.1f}\n"
        f"{tp2}\n"
        f"<b>Отмена сетапа, если:</b>\n"
        f"• цена дойдёт до TP1, не коснувшись CE\n"
        f"• вход не сработает до {fmt_msk(expire_at)}\n\n"
        f"<i>Вход можно подтверждать на 1m–5m (rejection от CE), но статистика бота "
        f"считает исполнение лимиткой по CE.</i>\n"
        f"⚠️ Не финансовая рекомендация. Риск на сделку - по вашему плану."
    )


def render_chart(sig: Dict) -> BytesIO:
    df, off = sig["df"], sig["offset"]
    n = len(df)
    long_ = sig["direction"] == "LONG"
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    tiny = (h.max() - l.min()) * 0.0015
    x_end = n + 10

    def px(pos: int) -> int:
        return min(max(pos - off, 0), n - 1)

    fig, ax = plt.subplots(figsize=(10, 6), dpi=110)
    for x in range(n):
        col = "#26a69a" if c[x] >= o[x] else "#ef5350"
        ax.vlines(x, l[x], h[x], color=col, linewidth=1)
        ax.add_patch(Rectangle((x - 0.3, min(o[x], c[x])), 0.6, max(abs(c[x] - o[x]), tiny), color=col))

    f0 = px(sig["fvg_i"] - 2)
    ax.add_patch(Rectangle((f0 - 0.4, sig["fvg_bottom"]), x_end - f0 + 0.4, sig["fvg_top"] - sig["fvg_bottom"],
                           color="#1e88e5", alpha=0.18, lw=0))
    ax.hlines(sig["level"], px(sig["level_pos"]), px(sig["k"]) + 1, colors="#8e24aa", linestyles=":", linewidth=1.2)
    ax.hlines(sig["ref"], px(sig["ref_pos"]), px(sig["m"]) + 1, colors="#fb8c00", linestyles=":", linewidth=1.2)

    for price, color, label in (
        (sig["entry"], "#2e7d32", "Entry (CE)"),
        (sig["stop"], "#c62828", "Stop"),
        (sig["tp1"], "#f9a825", "TP1"),
    ):
        ax.hlines(price, f0, x_end, colors=color, linestyles="--", linewidth=1.1)
        ax.text(x_end + 0.3, price, f"{label} {fp(price)}", color=color, va="center", fontsize=8)
    if sig.get("tp2"):
        ax.hlines(sig["tp2"], f0, x_end, colors="#f9a825", linestyles=":", linewidth=1)
        ax.text(x_end + 0.3, sig["tp2"], f"TP2 {fp(sig['tp2'])}", color="#f9a825", va="center", fontsize=8)

    k, m = px(sig["k"]), px(sig["m"])
    side = -1 if long_ else 1
    arrow = dict(arrowstyle="->", color="#555")
    lo_y, hi_y = ax.get_ylim()
    pad = (hi_y - lo_y) * 0.12  # запас под подписи
    ax.set_ylim(lo_y - pad, hi_y + pad)
    ax.annotate(f"Sweep {sig['level_label']}", xy=(k, l[k] if long_ else h[k]), xytext=(-48, 30 * side),
                textcoords="offset points", ha="center", fontsize=8, arrowprops=arrow)
    ax.annotate("MSS", xy=(m, c[m]), xytext=(-30, -30 * side), textcoords="offset points",
                ha="center", fontsize=8, arrowprops=arrow)
    ax.text(x_end - 0.3, sig["fvg_top"] if long_ else sig["fvg_bottom"], "FVG ", color="#1e88e5", fontsize=8,
            ha="right", va="bottom" if long_ else "top")

    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([df.index[i].tz_convert(MSK).strftime("%d.%m %H:%M") for i in ticks], fontsize=8)
    ax.set_xlim(-1, x_end + 12)
    ax.grid(alpha=0.25)
    ax.set_title(f"{sig['symbol']} {sig['direction']} | {LTF} | RR 1:{sig['rr']:.1f} (время МСК)")

    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


# ============================== TELEGRAM / LOOPS ==============================


def authorized(update) -> bool:
    chat = update.effective_chat
    return bool(chat) and str(chat.id) == str(CHAT_ID)


async def cmd_help(update, context) -> None:
    if not authorized(update):
        return
    await update.message.reply_text(
        "Команды:\n/stats - статистика сигналов\n/open - сетапы и активные сделки\n"
        f"\nHTF {HTF} / LTF {LTF}, MIN_RR {MIN_RR}, лимит {MAX_SIGNALS_PER_DAY}/день, "
        f"Kill Zones: {', '.join(ENABLED_KZ)} (время Нью-Йорка)."
    )


async def cmd_stats(update, context) -> None:
    if authorized(update):
        await update.message.reply_text(stats_text(context.application.bot_data["store"]), parse_mode="HTML")


async def cmd_open(update, context) -> None:
    if not authorized(update):
        return
    store: TradeStore = context.application.bot_data["store"]
    rows = [t for t in store.trades if t["status"] in ("pending", "active")]
    if not rows:
        await update.message.reply_text("Открытых сетапов нет.")
        return
    lines = [
        f"{'⏳' if t['status'] == 'pending' else '▶️'} {t['symbol']} {t['direction']} | вход {fp(t['entry'])} | "
        f"SL {fp(t['stop'])} | TP {fp(t['tp1'])}"
        for t in rows
    ]
    await update.message.reply_text("\n".join(lines))


async def send_signal(bot, sig: Dict) -> None:
    await bot.send_message(chat_id=CHAT_ID, text=signal_text(sig), parse_mode="HTML")
    try:
        photo = await asyncio.to_thread(render_chart, sig)
        await bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=f"Разметка {sig['symbol']} ({LTF})")
    except Exception as e:  # noqa: BLE001
        logger.warning("chart: %s", e)


async def scanner_loop(bot, ex, store: TradeStore) -> None:
    top: List[str] = []
    top_ts = 0.0
    first = True
    tf = tf_seconds(LTF)
    while True:
        if not first:  # дальше сканируем только после закрытия каждой LTF-свечи
            now_t = time.time()
            await asyncio.sleep(max(1.0, (now_t // tf + 1) * tf + CLOSE_DELAY_S - now_t))
        first = False
        try:
            now = utcnow()
            kz = kz_at(now)
            if not kz:
                logger.info("Вне Kill Zone")
                continue
            if store.signals_today(now) >= MAX_SIGNALS_PER_DAY:
                logger.info("Лимит сигналов на сегодня исчерпан")
                continue
            if not top or time.time() - top_ts > 1800:
                top = await asyncio.to_thread(fetch_top_symbols, ex)
                top_ts = time.time()
            btc_bias = await asyncio.to_thread(get_btc_bias, ex) if BTC_FILTER else None
            logger.info("Скан %s, пар: %d, BTC bias: %s", kz, len(top), btc_bias)

            for sym in top:
                if store.signals_today(utcnow()) >= MAX_SIGNALS_PER_DAY:
                    break
                try:
                    sig = await asyncio.to_thread(analyze_symbol, ex, sym, btc_bias)
                except Exception as e:  # noqa: BLE001
                    logger.warning("%s: %s", sym, e)
                    continue
                if not sig or store.blocked(sig, utcnow()):
                    continue
                await send_signal(bot, sig)
                store.add(make_trade(sig, utcnow()))
                logger.info("Сигнал: %s %s", sym, sig["direction"])
        except Exception:  # noqa: BLE001
            logger.exception("scanner")


EVENT_TEXT = {
    "filled": "🟢 {s} {d}: цена коснулась CE - вход по лимитке исполнен",
    "win": "✅ {s} {d}: TP1 достигнут ({r:+.2f}R)",
    "loss": "❌ {s} {d}: стоп ({r:+.2f}R)",
    "timeout": "⏱ {s} {d}: закрыто по таймауту ({r:+.2f}R)",
    "expired": "⌛ {s} {d}: вход так и не сработал, сетап снят",
    "missed": "🚫 {s} {d}: цена ушла к цели без входа, сетап отменён",
}


async def monitor_trades(bot, ex, store: TradeStore) -> None:
    for t in [x for x in store.trades if x["status"] in ("pending", "active")]:
        try:
            raw = await asyncio.to_thread(ex.fetch_ohlcv, t["symbol"], "1m", t["checked_ms"], 1000)
            now_ms = ms(utcnow())
            candles = [
                (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in raw
                if r[0] + 60_000 <= now_ms
            ]
            events = evaluate_trade(t, candles)
            store.save()
            for ev in events:
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=EVENT_TEXT[ev].format(s=t["symbol"], d=t["direction"], r=t["r"] or 0.0),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("monitor %s: %s", t["symbol"], e)


async def monitor_loop(bot, ex, store: TradeStore) -> None:
    last_alert = None
    while True:
        try:
            nxt = next_kz_start(utcnow())
            if nxt:
                name, start = nxt
                minutes = (start - utcnow()).total_seconds() / 60
                alert_key = f"{name}-{start.isoformat()}"
                if 0 < minutes <= KZ_ALERT_MIN and alert_key != last_alert:
                    last_alert = alert_key
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"⏰ Kill Zone «{name}» откроется через ~{int(minutes)} мин ({fmt_msk(start)})",
                    )
            await monitor_trades(bot, ex, store)
        except Exception:  # noqa: BLE001
            logger.exception("monitor")
        await asyncio.sleep(MONITOR_INTERVAL)


async def run_bot() -> None:
    from telegram.ext import Application, CommandHandler

    ex = make_exchange()
    store = TradeStore(TRADES_FILE)
    store.load()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.bot_data["store"] = store
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("open", cmd_open))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("ICT bot запущен: %s, HTF %s / LTF %s", MARKET_TYPE, HTF, LTF)
    try:
        await asyncio.gather(scanner_loop(app.bot, ex, store), monitor_loop(app.bot, ex, store))
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise SystemExit("Задайте переменные окружения TELEGRAM_TOKEN и CHAT_ID")
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
